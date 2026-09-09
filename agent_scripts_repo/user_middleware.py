"""FastMCP middleware for generated agents (lifted verbatim from the ADLC
new_host_agent middleware/user_middleware.py).

Validates the Keycloak bearer token and publishes the authenticated user's
email/name onto request.state for downstream MCP tools.
"""

from starlette.responses import Response
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token, get_http_request
import logging

logger = logging.getLogger("generated_agent")


def _first_claim_text(value) -> str:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item
        return ""
    if isinstance(value, str):
        return value
    return ""

class TokenNotFoundError(Exception):
    pass

class UserDetailsNotFoundError(Exception):
    pass

class UserMiddleware(Middleware):
    async def on_message(self, context: MiddlewareContext, call_next):
        try:
            request = get_http_request()
            user = request.scope.get("user", None)
            logger.debug(f"UserMiddleware: Retrieved user from request scope: {user}, type: {type(user)}")
            if not user or not isinstance(user, AuthenticatedUser):
                logger.error(f"UserMiddleware: Authenticated user not found in request scope")
                raise AuthorizationError("User is not Authenticated.")

            logger.info("UserMiddleware: Processing request to extract user information.")
            token = get_access_token()
            if token is None or token.token == '':
                raise TokenNotFoundError("Access token not found in the request context.")
            logger.debug(f"Access token retrieved from context: {token}")

            claims = token.claims or {}
            if not isinstance(claims, dict):
                raise UserDetailsNotFoundError("Token claims are missing or invalid.")

            verified_email = _first_claim_text(claims.get("verified_primary_email", ""))
            email_from_claim = _first_claim_text(claims.get("email", ""))
            upn = _first_claim_text(claims.get("upn", ""))
            unique_name = _first_claim_text(claims.get("unique_name", ""))
            email = verified_email or email_from_claim or upn or unique_name
            if not email:
                raise UserDetailsNotFoundError("Required user details (email) not found in token claims.")

            name = claims.get("name", "")
            if not name:
                logger.warning("Required user details (name) not found in token claims. Defaulting to empty string.")

            request.state.email = email
            request.state.name = name
            logger.debug(f"User details set in request state: email={email}, name={name}, request state={request.state.__dict__}")

            return await call_next(context)

        except AuthorizationError as e:
            logger.error(str(e))
            return Response("Unauthorized: User is not authenticated.", status_code=401)
        except TokenNotFoundError as e:
            logger.error(str(e))
            return Response("Authentication token not found.", status_code=401)
        except UserDetailsNotFoundError as e:
            logger.error(str(e))
            return Response("User details not found.", status_code=400)
