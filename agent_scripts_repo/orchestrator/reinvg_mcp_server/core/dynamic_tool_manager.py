import importlib.util
import sys
import inspect
import ast
import re
import time
import subprocess
import os
import logging
from pathlib import Path
from typing import Dict, List, Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from configparser import ConfigParser

config = ConfigParser()
config_path = Path(__file__).parent.parent.parent.parent / "config.ini"
config.read(config_path)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv() 
except ImportError:
    logger.warning("python-dotenv not installed, skipping .env loading")


class DynamicToolFileHandler(FileSystemEventHandler):
    """Handles file system events for the tools directory."""

    def __init__(self, manager):
        self.manager = manager
        # Track last event time for each file to prevent duplicate reloads
        self.last_event_time: Dict[str, float] = {}
        # Debounce delay in seconds
        self.debounce_delay = 1.0

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        filename = Path(event.src_path).name

        if filename.startswith("_"):
            return

        # Skip static files
        if self.manager._is_static_file(filename):
            return

        logger.info(f"[FileWatcher] 📄 NEW FILE: {filename}")
        self.manager.load_dynamic_tool(filename)

    def on_modified(self, event):
        if event.is_directory:
            return

        filename = Path(event.src_path).name

        # Handle .requirements.txt file changes
        if filename.endswith(".requirements.txt"):
            # Extract tool filename: weather_tool.requirements.txt → weather_tool.py
            tool_filename = filename.replace(".requirements.txt", ".py")

            # Skip if tool file doesn't exist
            tool_path = self.manager.tools_dir / tool_filename
            if not tool_path.exists():
                return

            logger.info(f"[FileWatcher] 📄 Requirements updated: {filename}")
            logger.info(f"[FileWatcher] 🔄 Reloading tool: {tool_filename}")

            # Reload the corresponding tool
            self.manager.reload_dynamic_tool(tool_filename)
            return

        # Handle .py file changes
        if not event.src_path.endswith(".py"):
            return

        # Skip __init__.py and __pycache__
        if filename.startswith("_"):
            return

        # Skip static files
        if self.manager._is_static_file(filename):
            return

        # Debounce: Check if we just processed this file recently
        current_time = time.time()
        last_time = self.last_event_time.get(filename, 0)

        if current_time - last_time < self.debounce_delay:
            # Too soon! Skip this reload to avoid duplicate processing
            logger.info(f"[FileWatcher] ⏳ Debouncing {filename} (ignoring duplicate event within {self.debounce_delay}s)")
            return

        # Update the last event time for this file
        self.last_event_time[filename] = current_time

        logger.info(f"[FileWatcher] 🔄 MODIFIED: {filename}")
        self.manager.reload_dynamic_tool(filename)

    def on_deleted(self, event):
        if event.is_directory or not event.src_path.endswith(".py"):
            return

        filename = Path(event.src_path).name

        # Skip __init__.py and __pycache__
        if filename.startswith("_"):
            return

        # Skip static files
        if self.manager._is_static_file(filename):
            return

        logger.info(f"[FileWatcher] 🗑️ DELETED: {filename}")
        self.manager.unload_dynamic_tool(filename)


class DynamicToolManager:
    """Manages dynamic tool loading and reloading from the tools directory."""

    # NOTE: USE_SUFFIX_DETECTION is now controlled via environment variable (see __init__)
    # Old class variable kept for reference but not used 


    def __init__(self, mcp_instance, tools_directory: str = "tools", static_files: List[str] = None):
        """
        Initialize the Dynamic Tool Manager.

        Args:
            mcp_instance: The FastMCP instance to register tools with
            tools_directory: Path to the tools directory (relative or absolute)
            static_files: List of static tool filenames (imported with @mcp.tool decorator)
                         These files will be skipped by the dynamic loader
        """
        self.mcp = mcp_instance
        _td = Path(tools_directory)
        # Resolve relative paths to the MCP server root so the watchdog inotify
        # watcher finds the correct directory on Cloud Run (CWD is /app, not this file's parent).
        if not _td.is_absolute():
            self.tools_dir = Path(__file__).resolve().parent.parent / tools_directory
        else:
            self.tools_dir = _td

        # Configure auto-install behavior from environment variable
        env_value = config.get("config", "auto_install_dependencies", fallback="true").lower()
        valid_true = ("true", "1", "yes", "on")
        valid_false = ("false", "0", "no", "off")

        if env_value in valid_true:
            self.auto_install_dependencies = True
        elif env_value in valid_false:
            self.auto_install_dependencies = False
        else:
            # Invalid value - warn and default to True (safe default)
            logger.warning(f"[DynamicToolManager] ⚠️  Invalid AUTO_INSTALL_DEPENDENCIES='{env_value}', defaulting to True")
            self.auto_install_dependencies = True

        # Configure suffix detection from environment variable
        suffix_env = config.get("config", "use_suffix_detection", fallback="false").lower()

        if suffix_env in valid_true:
            self.use_suffix_detection = True
        elif suffix_env in valid_false:
            self.use_suffix_detection = False
        else:
            # Invalid value - warn and default to False (modern default)
            logger.warning(f"[DynamicToolManager] ⚠️  Invalid USE_SUFFIX_DETECTION='{suffix_env}', defaulting to False")
            self.use_suffix_detection = False

        # Track which files are static (imported with @mcp.tool decorator)
        self.static_files: Set[str] = set(static_files or [])

        # Track dynamically loaded files and their registered tools
        # {filename: [tool_name1, tool_name2, ...]}
        self.dynamic_files: Dict[str, List[str]] = {}

        # Track which tool comes from which file 
        # {tool_name: filename}
        self.tool_to_file: Dict[str, str] = {}

        self.loaded_modules: Dict[str, object] = {}

        self.observer = None

        logger.info("=" * 60)
        logger.info("[DynamicToolManager] Initialized")
        logger.info(f"[DynamicToolManager] Tools directory: {self.tools_dir.absolute()}")
        logger.info(f"[DynamicToolManager] Auto-install dependencies: {'ENABLED' if self.auto_install_dependencies else 'DISABLED'}")
        logger.info(f"[DynamicToolManager] Suffix detection (_tool): {'ENABLED' if self.use_suffix_detection else 'DISABLED'}")
        if self.static_files:
            logger.info(f"[DynamicToolManager] Static files (will be SKIPPED): {sorted(self.static_files)}")
        logger.info("=" * 60)

    def _is_static_file(self, filename: str) -> bool:
        """Check if this is a static file that should be skipped."""
        return filename in self.static_files

    def _extract_imports_from_file(self, file_path: Path) -> Set[str]:
        """
        Extract all import statements from a Python file.

        Args:
            file_path: Path to Python file

        Returns:
            Set of package names (e.g., {'requests', 'pydantic'})
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()

            tree = ast.parse(source_code, filename=str(file_path))
            imports = set()

            for node in ast.walk(tree):
                # Handle: import requests
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        package = alias.name.split('.')[0]
                        imports.add(package)

                # Handle: from pydantic import BaseModel
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        package = node.module.split('.')[0]
                        imports.add(package)

            return imports

        except Exception as e:
            logger.error(f"[DynamicToolManager] ❌ Error extracting imports: {e}")
            return set()

    def _is_stdlib_package(self, package_name: str) -> bool:
        """
        Check if a package is part of Python standard library.

        Args:
            package_name: Name of package

        Returns:
            True if stdlib, False if third-party
        """
        stdlib_packages = {
            'os', 'sys', 'json', 'datetime', 'time', 'math', 'random',
            'typing', 're', 'collections', 'itertools', 'functools',
            'pathlib', 'subprocess', 'threading', 'asyncio', 'io',
            'logging', 'unittest', 'argparse', 'configparser', 'csv',
            'xml', 'html', 'http', 'urllib', 'email', 'base64', 'hashlib',
            'uuid', 'secrets', 'string', 'textwrap', 'difflib', 'pprint',
            'warnings', 'traceback', 'inspect', 'ast', 'dis', 'gc',
            'copy', 'pickle', 'shelve', 'sqlite3', 'gzip', 'zipfile',
            'tarfile', 'tempfile', 'shutil', 'glob', 'fnmatch',
            'abc', 'dataclasses', 'enum', 'contextlib', 'weakref',
        }

        return package_name in stdlib_packages

    def _auto_generate_requirements_file(self, filename: str) -> Path:
        """
        Auto-generate requirements.txt file for a tool.

        Args:
            filename: Name of Python file (e.g., 'weather_tool.py')

        Returns:
            Path to generated requirements file, or None if no deps
        """
        file_path = self.tools_dir / filename

        logger.info(f"[DynamicToolManager] 🛠️ AUTO-GENERATING requirements file...")
        logger.info(f"[DynamicToolManager]")

        # Extract all imports
        logger.info(f"[DynamicToolManager] 🔍 Scanning imports in {filename}...")
        all_imports = self._extract_imports_from_file(file_path)

        # Filter out stdlib packages
        third_party_imports = set()
        for pkg in all_imports:
            if self._is_stdlib_package(pkg):
                logger.info(f"[DynamicToolManager]   ⚙️  {pkg} (stdlib - skipped)")
            else:
                third_party_imports.add(pkg)
                logger.info(f"[DynamicToolManager]   🛠️ {pkg} (third-party)")

        if not third_party_imports:
            logger.info(f"[DynamicToolManager] 📝  No third-party imports found")
            logger.info(f"[DynamicToolManager]")
            return None

        # Generate requirements file
        base_name = filename.replace('.py', '')
        req_filename = f"{base_name}.requirements.txt"
        req_path = self.tools_dir / req_filename

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        content = f"# Auto-generated requirements for {filename}\n"
        content += f"# Generated on: {timestamp}\n"
        content += f"# Edit this file to add version constraints\n\n"

        for pkg in sorted(third_party_imports):
            content += f"{pkg}\n"

        # Write to file
        with open(req_path, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"[DynamicToolManager]")
        logger.info(f"[DynamicToolManager] ✅ Created: {req_filename}")
        logger.info(f"[DynamicToolManager]")

        return req_path

    def _get_requirements_file(self, filename: str) -> Path:
        """
        Get requirements file path for a tool.

        Args:
            filename: Name of Python file (e.g., 'weather_tool.py')

        Returns:
            Path to requirements file if exists, None otherwise
        """
        base_name = filename.replace('.py', '')
        req_filename = f"{base_name}.requirements.txt"
        req_path = self.tools_dir / req_filename

        if req_path.exists():
            return req_path
        return None

    def _get_missing_packages(self, requirements_file: Path) -> List[str]:
        """
        Read requirements file and return packages that aren't installed.

        Args:
            requirements_file: Path to requirements.txt

        Returns:
            List of missing package requirement strings
        """
        with open(requirements_file, 'r') as f:
            requirements = f.read().splitlines()

        missing = []
        for req in requirements:
            # Skip empty lines and comments
            req = req.strip()
            if not req or req.startswith('#'):
                continue

            # Extract package name (before ==, >=, etc.)
            package_name = re.split(r'[=<>!]', req)[0].strip()

            # Check if installed
            try:
                importlib.import_module(package_name)
                # Already installed - skip
            except ImportError:
                missing.append(req)

        return missing

    def _install_requirements(self, requirements_file: Path) -> bool:
        """
        Install packages from requirements file and update pyproject.toml.

        Args:
            requirements_file: Path to requirements.txt

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"[DynamicToolManager] 🛠️ Installing packages from {requirements_file.name}...")

            # Detect if using uv or standard pip
            # Try uv first (check if uv command is available)
            try:
                uv_check = subprocess.run(
                    ["uv", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                uv_available = (uv_check.returncode == 0)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                uv_available = False

            if uv_available:
                # Use uv pip install
                logger.info(f"[DynamicToolManager] Running: uv pip install -r {requirements_file.name}")
                logger.info(f"[DynamicToolManager]")

                result = subprocess.run(
                    ["uv", "pip", "install", "-r", str(requirements_file)],
                    capture_output=False,  # Show pip output
                    text=True,
                    timeout=300  # 5 minute timeout
                )
            else:
                # Fall back to standard pip
                logger.info(f"[DynamicToolManager] Running: pip install -r {requirements_file.name}")
                logger.info(f"[DynamicToolManager]")

                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(requirements_file)],
                    capture_output=False,  # Show pip output
                    text=True,
                    timeout=300  # 5 minute timeout
                )

            logger.info(f"[DynamicToolManager]")

            if result.returncode == 0:
                logger.info(f"[DynamicToolManager] ✅ Packages installed successfully!")

                # Update pyproject.toml
                self._update_pyproject_toml(requirements_file)

                # Invalidate import cache
                logger.info(f"[DynamicToolManager] 🔄 Refreshing import cache...")
                importlib.invalidate_caches()
                logger.info(f"[DynamicToolManager] ✅ Import cache refreshed")
                logger.info(f"[DynamicToolManager]")

                return True
            else:
                logger.error(f"[DynamicToolManager] ❌ Installation failed (exit code: {result.returncode})")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"[DynamicToolManager] ❌ Installation timeout (>5 minutes)")
            return False
        except Exception as e:
            logger.error(f"[DynamicToolManager] ❌ Installation error: {e}")
            return False

    def _update_pyproject_toml(self, requirements_file: Path):
        """
        Add installed packages to pyproject.toml [project.dependencies].

        Args:
            requirements_file: Path to requirements.txt file
        """
        try:
            import toml
        except ImportError:
            logger.warning(f"[DynamicToolManager] ⚠️  'toml' package not installed, skipping pyproject.toml update")
            return

        try:
            pyproject_path = Path("pyproject.toml")

            if not pyproject_path.exists():
                logger.warning(f"[DynamicToolManager] ⚠️  pyproject.toml not found, skipping update")
                return

            # Read requirements
            with open(requirements_file, 'r') as f:
                new_packages = [
                    line.strip() for line in f
                    if line.strip() and not line.strip().startswith('#')
                ]

            if not new_packages:
                return

            # Read pyproject.toml
            with open(pyproject_path, 'r') as f:
                data = toml.load(f)

            # Get current dependencies
            current_deps = data.get("project", {}).get("dependencies", [])

            # Add new packages (avoid duplicates)
            added = []
            for pkg in new_packages:
                # Extract package name for comparison
                pkg_name = re.split(r'[=<>!]', pkg)[0].strip()

                # Check if already exists
                already_exists = any(
                    re.split(r'[=<>!]', dep)[0].strip() == pkg_name
                    for dep in current_deps
                )

                if not already_exists:
                    current_deps.append(pkg)
                    added.append(pkg)

            if added:
                # Update dependencies
                data["project"]["dependencies"] = current_deps

                # Write back
                with open(pyproject_path, 'w') as f:
                    toml.dump(data, f)

                logger.info(f"[DynamicToolManager] ✅ Updated pyproject.toml:")
                for pkg in added:
                    logger.info(f"[DynamicToolManager]   + {pkg}")

        except Exception as e:
            logger.warning(f"[DynamicToolManager] ⚠️  Error updating pyproject.toml: {e}")

    def _find_tool_decorated_functions(self, file_path: Path) -> List[str]:
        """
        Parse a Python file and find all functions decorated with @tool.

        Args:
            file_path: Path to the Python file to parse

        Returns:
            List of function names that have @tool decorator
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()

            # Parse the Python file into an Abstract Syntax Tree
            tree = ast.parse(source_code, filename=str(file_path))

            tool_function_names = []

            # Walk through all nodes in the AST
            for node in ast.walk(tree):
                # Look for both sync and async function definitions
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Check if function has any decorators
                    for decorator in node.decorator_list:
                        # Handles: @tool
                        if isinstance(decorator, ast.Name) and decorator.id == 'tool':
                            tool_function_names.append(node.name)
                            break
                        # Handles: @module.tool (e.g., @mcp.tool, @fastmcp.tool)
                        elif isinstance(decorator, ast.Attribute) and decorator.attr == 'tool':
                            tool_function_names.append(node.name)
                            break
                        # Handles: @module.tool() or @tool() (called decorator with parens)
                        elif isinstance(decorator, ast.Call):
                            func = decorator.func
                            if isinstance(func, ast.Attribute) and func.attr == 'tool':
                                tool_function_names.append(node.name)
                                break
                            elif isinstance(func, ast.Name) and func.id == 'tool':
                                tool_function_names.append(node.name)
                                break

            return tool_function_names

        except SyntaxError as e:
            logger.error(f"[DynamicToolManager] ❌ Syntax error in {file_path.name}:")
            logger.error(f"[DynamicToolManager]    Line {e.lineno}: {e.msg}")
            if e.text:
                logger.error(f"[DynamicToolManager]    {e.text.strip()}")
            return []
        except Exception as e:
            logger.error(f"[DynamicToolManager] ❌ Error parsing {file_path.name}: {str(e)}")
            return []

    def _find_tool_suffixed_functions(self, module) -> List[str]:
        """
        Find functions in a module that end with '_tool' suffix.
        This is the OLD detection method for backward compatibility.

        Args:
            module: The loaded Python module

        Returns:
            List of function names ending with '_tool'
        """
        tool_names = []
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.endswith("_tool"):
                tool_names.append(name)
        return tool_names

    def load_dynamic_tool(self, filename: str):
        """
        Load a dynamic tool from a Python file.

        Args:
            filename: Name of the .py file in the tools directory
        """
        # Skip if already loaded
        if filename in self.dynamic_files:
            logger.info(f"[DynamicToolManager] ⏭️ Skipping {filename} (already loaded)")
            return

        file_path = self.tools_dir / filename

        if not file_path.exists():
            logger.warning(f"[DynamicToolManager] ⚠️ File not found: {filename}")
            return

        logger.info(f"[DynamicToolManager] 🚀 Loading DYNAMIC tool: {filename}")

        # ========== CONDITIONAL: INSTALL PACKAGES BEFORE LOADING ==========
        if self.auto_install_dependencies:
            req_file = self._get_requirements_file(filename)

            if not req_file:
                # No requirements file - try to auto-generate
                req_file = self._auto_generate_requirements_file(filename)

            if req_file:
                # Check which packages are missing
                missing = self._get_missing_packages(req_file)

                if missing:
                    logger.info(f"[DynamicToolManager] ⚠️ Missing packages detected:")
                    for pkg in missing:
                        logger.info(f"[DynamicToolManager]   - {pkg}")
                    logger.info(f"[DynamicToolManager]")

                    # Show 3-second review window
                    logger.info(f"[DynamicToolManager] ⚠️ You can edit: {req_file}")
                    logger.info(f"[DynamicToolManager]    Press Ctrl+C to cancel installation")
                    logger.info(f"[DynamicToolManager]")

                    for i in range(3, 0, -1):
                        logger.info(f"[DynamicToolManager] ⏳ Installing in: {i} second{'s' if i > 1 else ''}...")
                        time.sleep(1)

                    logger.info(f"[DynamicToolManager]")

                    # Install packages
                    success = self._install_requirements(req_file)

                    if not success:
                        logger.error(f"[DynamicToolManager] ❌ Cannot load {filename} - package installation failed")
                        return
                else:
                    logger.info(f"[DynamicToolManager] ✅ All dependencies already installed")
                    logger.info(f"[DynamicToolManager]")
        # ==================================================================

        try:
            # Load the module dynamically
            spec = importlib.util.spec_from_file_location(
                filename[:-3],  # Remove .py extension
                file_path
            )

            if spec is None or spec.loader is None:
                logger.error(f"[DynamicToolManager] ❌ Could not create spec for {filename}")
                return

            module = importlib.util.module_from_spec(spec)

            # Load module (packages should be installed now)
            try:
                spec.loader.exec_module(module)
            except ImportError as e:
                if not self.auto_install_dependencies:
                    # Auto-install is disabled - show helpful message
                    logger.error(f"[DynamicToolManager] ❌ Import error in {filename}: {e}")
                    logger.info(f"[DynamicToolManager] ⚠️ Missing packages? Either:")
                    logger.info(f"[DynamicToolManager]    1. Install manually: pip install <package>")
                    logger.info(f"[DynamicToolManager]    2. Enable auto-install: set AUTO_INSTALL_DEPENDENCIES=true in .env")
                    return  # Skip this tool, continue with others
                else:
                    # Auto-install was enabled but still failed
                    logger.error(f"[DynamicToolManager] ❌ Import error in {filename}: {e}")
                    logger.error(f"[DynamicToolManager] ❌ Package installation may have failed")
                    logger.error(f"[DynamicToolManager] ❌ Check requirements and try again")
                    raise

            self.loaded_modules[filename] = module

            # Collect tool function names using different detection methods
            tool_function_names = []

            # Method 1: Find functions with @tool decorator (NEW)
            decorator_tool_names = self._find_tool_decorated_functions(file_path)
            tool_function_names.extend(decorator_tool_names)

            # Method 2: Find functions ending with _tool (OLD - for backward compatibility)
            # Control via USE_SUFFIX_DETECTION environment variable in .env file
            if self.use_suffix_detection:
                suffix_tool_names = self._find_tool_suffixed_functions(module)
                tool_function_names.extend(suffix_tool_names)

            # Remove duplicates (in case a function has both @tool and _tool suffix)
            tool_function_names = list(set(tool_function_names))

            if not tool_function_names:
                logger.warning(f"[DynamicToolManager] ⚠️ No tool functions found in {filename}")
                logger.info(f"[DynamicToolManager]    Looking for: @tool decorator or _tool suffix")
                return

            # Get the actual function/callable objects from the loaded module
            tool_functions = []
            for name, obj in inspect.getmembers(module):
                if name in tool_function_names:
                    # Skip if not callable (must be invokable like a function)
                    if not callable(obj):
                        logger.warning(f"[DynamicToolManager] ⚠️ Skipping {name} - not callable (type: {type(obj).__name__})")
                        continue

                    logger.info(f"[DynamicToolManager]   Found tool: {name} (type: {type(obj).__name__})")

                    # TIER 1: Already a plain function? Use it directly
                    if inspect.isfunction(obj):
                        logger.info(f"[DynamicToolManager]     ✅ Already a plain function")
                        tool_functions.append((name, obj))
                        continue

                    # TIER 2: Try standard unwrapping (handles __wrapped__ convention)
                    unwrapped_func = obj
                    try:
                        unwrapped_func = inspect.unwrap(obj)
                        if unwrapped_func != obj and inspect.isfunction(unwrapped_func):
                            logger.info(f"[DynamicToolManager]     🔄 Unwrapped via __wrapped__ to: {type(unwrapped_func).__name__}")
                            tool_functions.append((name, unwrapped_func))
                            continue
                    except (ValueError, AttributeError, TypeError) as e:
                        # Unwrapping failed, continue to next tier
                        logger.warning(f"[DynamicToolManager] Unwrapping failed for {name}, trying next method...")

                    # TIER 3: Try common decorator patterns (langsmith, langchain, etc.)
                    if not inspect.isfunction(unwrapped_func):
                        logger.info(f"[DynamicToolManager]     ⚙️ Attempting to extract function from wrapper...")
                        extracted_func = None

                        # Try common attributes where decorators store the original function
                        for attr in ['func', '_func', 'run', 'invoke']:
                            if hasattr(unwrapped_func, attr):
                                candidate = getattr(unwrapped_func, attr)
                                if callable(candidate) and inspect.isfunction(candidate):
                                    extracted_func = candidate
                                    logger.info(f"[DynamicToolManager]     🔧 Extracted function from wrapper.{attr}")
                                    break

                        if extracted_func:
                            tool_functions.append((name, extracted_func))
                            continue

                    # TIER 4: Use wrapper directly (will fail gracefully if incompatible)
                    logger.warning(f"[DynamicToolManager]     ⚠️ Could not extract function, using wrapper directly")
                    logger.warning(f"[DynamicToolManager]     ⚠️ Registration may fail if wrapper is incompatible with FastMCP")
                    tool_functions.append((name, unwrapped_func))

            if not tool_functions:
                logger.warning(f"[DynamicToolManager] ⚠️ Could not load tool functions from {filename}")
                return

            # Register each tool function
            registered_tools = [] 
            for tool_name, tool_func in tool_functions:
                try:
                    # Get docstring for description
                    docstring = tool_func.__doc__ or f"Tool: {tool_name}"

                    # Check for naming conflicts with existing tools
                    # Check multiple registries where FastMCP might store tools
                    existing_tools = set()

                    # Check _tools list
                    if hasattr(self.mcp, "_tools"):
                        existing_tools.update(t.name for t in getattr(self.mcp, "_tools", []))

                    # Check _tool_manager._tools dict (where FastMCP actually stores tools)
                    if hasattr(self.mcp, "_tool_manager"):
                        tool_mgr = self.mcp._tool_manager
                        if hasattr(tool_mgr, "_tools") and isinstance(tool_mgr._tools, dict):
                            existing_tools.update(tool_mgr._tools.keys())

                    if tool_name in existing_tools:
                        # Show which file is being replaced
                        old_file = self.tool_to_file.get(tool_name, "unknown")
                        logger.warning(f"[DynamicToolManager] ⚠️ Tool '{tool_name}' from {filename} is REPLACING the one from {old_file}")

                        # Force remove from all registries before reloading
                        if hasattr(self.mcp, "_tools"):
                            self.mcp._tools[:] = [t for t in self.mcp._tools if t.name != tool_name]

                        if hasattr(self.mcp, "_tools_by_name") and tool_name in self.mcp._tools_by_name:
                            del self.mcp._tools_by_name[tool_name]

                        # Remove from _tool_manager._tools (THE ACTUAL REGISTRY)
                        if hasattr(self.mcp, "_tool_manager"):
                            tool_mgr = self.mcp._tool_manager
                            if hasattr(tool_mgr, "_tools") and isinstance(tool_mgr._tools, dict):
                                tool_mgr._tools.pop(tool_name, None)

                        # Check other tool registries
                        for attr_name in dir(self.mcp):
                            if "tool" in attr_name.lower():
                                attr_value = getattr(self.mcp, attr_name, None)
                                if isinstance(attr_value, dict) and tool_name in attr_value:
                                    attr_value.pop(tool_name, None)

                    # Dynamically apply the @mcp.tool() decorator
                    decorated_func = self.mcp.tool(name=tool_name)(tool_func)

                    # Update the reverse mapping: tool_name → filename
                    self.tool_to_file[tool_name] = filename

                    registered_tools.append(tool_name)
                    logger.info(f"[DynamicToolManager] ✅ Registered tool: {tool_name}")

                except Exception as e:
                    logger.error(f"[DynamicToolManager] ❌ Error registering tool '{tool_name}': {str(e)}")
                    import traceback
                    traceback.print_exc()
                    continue

            if registered_tools:
                self.dynamic_files[filename] = registered_tools
                logger.info(f"[DynamicToolManager] ✅ Loaded {len(registered_tools)} tool(s) from {filename}: {registered_tools}\n")
            else:
                logger.error(f"[DynamicToolManager] ❌ Failed to register any tools from {filename}\n")

        except SyntaxError as e:
            logger.error(f"[DynamicToolManager] ❌ Syntax error in {filename}:")
            logger.error(f"[DynamicToolManager]    Line {e.lineno}: {e.msg}")
            logger.error(f"[DynamicToolManager]    {e.text}")

        except Exception as e:
            logger.error(f"[DynamicToolManager] ❌ Error loading {filename}: {str(e)}")
            import traceback
            traceback.print_exc()

    def reload_dynamic_tool(self, filename: str):
        """
        Reload a previously loaded dynamic tool.

        Args:
            filename: Name of the .py file in the tools directory
        """
        logger.info(f"[DynamicToolManager] 🔄 Reloading: {filename}")

        # First unload if it was already loaded
        if filename in self.dynamic_files:
            self.unload_dynamic_tool(filename)

        # Then load it again
        self.load_dynamic_tool(filename)

        # Show updated mapping after reload
        self.print_tool_mapping_table()

    def unload_dynamic_tool(self, filename: str):
        """
        Unload a dynamically loaded tool.

        Args:
            filename: Name of the .py file in the tools directory
        """
        if filename not in self.dynamic_files:
            logger.info(f"[DynamicToolManager] ⚠️ Tool {filename} was not loaded (nothing to unload)")
            return

        logger.info(f"[DynamicToolManager] 🔄 Unloading: {filename}")

        try:
            tool_names = self.dynamic_files.pop(filename, [])

            if not tool_names:
                logger.info(f"[DynamicToolManager] ⚠️ No tools to unload from {filename}")
                return

            # Create a set of tool names for faster lookup
            tool_names_set = set(tool_names)

            # Remove from reverse mapping (tool_name → filename)
            for tool_name in tool_names_set:
                self.tool_to_file.pop(tool_name, None)

            # Remove tools from MCP instance - clear all FastMCP internal registries
            # 1. Remove from _tools list (modify in-place to avoid reference issues)
            # Filter all tools at once instead of looping per tool
            if hasattr(self.mcp, "_tools") and self.mcp._tools:
                filtered_tools = [t for t in self.mcp._tools if t.name not in tool_names_set]
                self.mcp._tools[:] = filtered_tools

            # 2. Remove from _tools_by_name dict if it exists
            if hasattr(self.mcp, "_tools_by_name"):
                for tool_name in tool_names_set:
                    self.mcp._tools_by_name.pop(tool_name, None)

            # 3. Remove from _tool_manager._tools dict (THE ACTUAL REGISTRY)
            # This is where FastMCP's ToolManager stores the tools
            if hasattr(self.mcp, "_tool_manager"):
                tool_mgr = self.mcp._tool_manager
                if hasattr(tool_mgr, "_tools") and isinstance(tool_mgr._tools, dict):
                    for tool_name in tool_names_set:
                        tool_mgr._tools.pop(tool_name, None)

            # 4. Remove from any other internal FastMCP registries
            # Check for other common attribute names that might store tools
            for attr_name in dir(self.mcp):
                if "tool" in attr_name.lower():
                    attr_value = getattr(self.mcp, attr_name, None)

                    # Handle dict-like registries
                    if isinstance(attr_value, dict):
                        for tool_name in tool_names_set:
                            attr_value.pop(tool_name, None)

                    # Handle list-like registries (just in case)
                    elif isinstance(attr_value, list):
                        try:
                            # Try to filter out tools by name attribute
                            filtered = [item for item in attr_value if not (hasattr(item, 'name') and item.name in tool_names_set)]
                            if len(filtered) < len(attr_value):
                                # Only update if something was actually removed
                                attr_value[:] = filtered
                        except (TypeError, AttributeError) as e:
                            logger.warning(f"[DynamicToolManager] Could not filter list attribute '{attr_name}' for tools (items may not have 'name' attribute): {e}")

            # Remove the module from sys.modules to force reimport
            module_name = filename[:-3]  # Remove .py extension
            if module_name in sys.modules:
                del sys.modules[module_name]
                logger.info(f"[DynamicToolManager] 🗑️ Removed module from sys.modules: {module_name}")

            # Remove from loaded modules cache
            if filename in self.loaded_modules:
                del self.loaded_modules[filename]

            for tool_name in tool_names:
                logger.info(f"[DynamicToolManager] ❌ Unregistered tool: {tool_name}")

        except Exception as e:
            logger.error(f"[DynamicToolManager] ❌ Error unloading {filename}: {str(e)}")
            import traceback
            traceback.print_exc()

    def print_tool_mapping_table(self):
        """Display a formatted table showing which tools are active and their source files."""
        if not self.tool_to_file:
            logger.info("[DynamicToolManager] No dynamic tools currently loaded")
            return

        # Sort tools alphabetically for consistent display
        sorted_tools = sorted(self.tool_to_file.items())

        # Calculate column widths
        max_tool_name_len = max(len(tool_name) for tool_name, _ in sorted_tools)
        max_file_name_len = max(len(filename) for _, filename in sorted_tools)

        # Ensure minimum column widths for headers
        tool_col_width = max(max_tool_name_len, len("Tool Name")) + 2
        file_col_width = max(max_file_name_len, len("Source File")) + 2

        # Total table width
        table_width = tool_col_width + file_col_width + 3  # 3 for separators

        # Print table
        logger.info("\n" + "╔" + "═" * table_width + "╗")
        title = "ACTIVE DYNAMIC TOOLS MAPPING"
        padding = (table_width - len(title)) // 2
        logger.info("║" + " " * padding + title + " " * (table_width - len(title) - padding) + "║")
        logger.info("╠" + "═" * table_width + "╣")

        # Header row
        header = f"║ {'Tool Name':<{tool_col_width}} ║ {'Source File':<{file_col_width}} ║"
        logger.info(header)
        logger.info("╠" + "═" * (tool_col_width + 2) + "╬" + "═" * (file_col_width + 2) + "╣")

        # Data rows
        for tool_name, filename in sorted_tools:
            row = f"║ {tool_name:<{tool_col_width}} ║ {filename:<{file_col_width}} ║"
            logger.info(row)

        # Footer
        logger.info("╚" + "═" * table_width + "╝")
        logger.info(f"Total: {len(sorted_tools)} active dynamic tool(s)\n")

    def load_all_dynamic_tools(self):
        """
        Load all non-static tool files at startup.
        This ensures previously-added dynamic tools persist across server restarts.
        """
        logger.info("\n[DynamicToolManager] Loading existing dynamic tools...")

        py_files = list(self.tools_dir.glob("*.py"))

        if not py_files:
            logger.info("[DynamicToolManager] No tool files found\n")
            return

        dynamic_tools_to_load = []

        # Find all non-static .py files
        for file_path in py_files:
            filename = file_path.name

            # Skip special files
            if filename.startswith("_"):
                continue

            # Skip static files
            if self._is_static_file(filename):
                continue

            dynamic_tools_to_load.append(filename)

        if not dynamic_tools_to_load:
            logger.info("[DynamicToolManager] No dynamic tools found (only static tools exist)\n")
            return

        # Load each dynamic tool
        logger.info(f"[DynamicToolManager] Found {len(dynamic_tools_to_load)} dynamic tool file(s):")
        for filename in sorted(dynamic_tools_to_load):
            logger.info(f"[DynamicToolManager]   - {filename}")

        for filename in sorted(dynamic_tools_to_load):
            self.load_dynamic_tool(filename)

        # Display summary table of loaded tools
        self.print_tool_mapping_table()

    def start_watching(self):
        """Start the file system watcher for the tools directory."""
        logger.info("[DynamicToolManager] 👁️ File watcher STARTED")
        logger.info(f"[DynamicToolManager] Monitoring: {self.tools_dir.absolute()}\n")

        handler = DynamicToolFileHandler(self)
        self.observer = Observer()
        self.observer.schedule(handler, str(self.tools_dir), recursive=False)
        self.observer.start()

    def stop_watching(self):
        """Stop the file system watcher."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("[DynamicToolManager] 👁️ File watcher STOPPED\n\n")
