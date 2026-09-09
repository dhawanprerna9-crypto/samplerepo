# Gap Analysis Report

## Gap 1: Account statement PDF request and output contract undefined

**Functional Area:** Download account statement in PDF format  |  **Affected Component:** Statement generation service / FR-001  |  **Category:** inputs_outputs  |  **Severity:** blocker

**Description:** The BRD states that users can generate and download account statements in a secure, structured PDF format by selecting a custom date range, and that the PDF should include user information, account number, transaction history, and summary totals. However, it does not define the exact request inputs, response shape, or delivery pattern for this feature. It is unclear whether the Python code should expose an API endpoint, return a binary file, generate a temporary download URL, or persist generated statements for later retrieval. Without a precise input/output contract, an autonomous coding agent would have to invent the interface and payloads for the core statement-download workflow.

**Missing / Under-specified:**
- statement download request fields
- date range format and validation rules
- PDF delivery method
- response payload schema
- statement retrieval vs on-demand generation behavior

**Code Generation Impact:** The code generator cannot determine the function or endpoint signature for generating and returning account statement PDFs.

**Assumption if unresolved:** The agent would assume a REST endpoint that accepts account_id, start_date, and end_date and returns a PDF file response or presigned URL.

**Why this is a gap:** FR-001 describes the business intent and broad content of the PDF but not the executable interface contract required for Python implementation. Different interface choices would lead to materially different code structure, storage handling, and tests.

## Gap 2: Statement PDF data model and summary calculations unspecified

**Functional Area:** Download account statement in PDF format  |  **Affected Component:** Statement formatter and transaction retrieval workflow / FR-001  |  **Category:** data_model  |  **Severity:** high

**Description:** The BRD says the statement PDF must contain user information, account number, detailed transaction history, and summary totals in a standardized layout. It does not specify the fields that make up user information or transaction history, the transaction record schema, or how summary totals should be calculated. There is no definition of whether totals mean opening balance, closing balance, credits, debits, fees, or net movement, and no currency or formatting rules are provided. This prevents deterministic generation of Python data models and PDF rendering logic for the statement feature.

**Missing / Under-specified:**
- statement header field list
- transaction line-item fields
- summary total definitions
- currency and amount formatting rules
- opening and closing balance calculation rules

**Code Generation Impact:** The code generator cannot define the statement schema or compute the values that must appear in the PDF.

**Assumption if unresolved:** The agent would assume a common bank statement structure with transaction date, description, debit, credit, balance, and aggregate debit/credit totals.

**Why this is a gap:** The document identifies high-level content only, not the concrete fields and calculations needed for implementation. For generated code, these details are essential because data structures, queries, and PDF templates depend directly on them.

## Gap 3: Digital wallet integration API contracts not identified

**Functional Area:** Integrate digital wallets for seamless transactions  |  **Affected Component:** Wallet integration service for Google Pay, Apple Pay, and PayPal / FR-002  |  **Category:** interfaces_apis  |  **Severity:** blocker

**Description:** The BRD requires integration of major digital wallets such as Google Pay, Apple Pay, and PayPal, with support for linking wallets, showing balances and histories, and enabling transfers, bill payments, and purchases. However, it does not identify the actual APIs, provider capabilities, endpoint contracts, authentication scopes, or whether each named wallet supports the same operations in this banking context. The statement that OAuth 2.0 and biometric verification will be used is too broad to implement provider-specific flows. Without explicit external service contracts, a Python agent cannot generate correct integration code and would be forced to guess unsupported or incompatible API behavior.

**Missing / Under-specified:**
- provider API names and versions
- wallet-specific supported operations
- endpoint URLs or SDK choice
- OAuth scopes and token flow details
- provider credential requirements

**Code Generation Impact:** The code generator cannot implement wallet linking or transaction calls because the external provider contracts are undefined.

**Assumption if unresolved:** The agent would assume generic REST APIs for all wallet providers with similar OAuth 2.0 authorization code flows and balance/transaction endpoints.

**Why this is a gap:** Named wallet brands alone are not enough to generate runnable integration code. Real implementations vary substantially by provider, market, and permitted use case, so guessing would likely produce nonfunctional code.

## Gap 4: Wallet transaction business rules and limits missing

**Functional Area:** Integrate digital wallets for seamless transactions  |  **Affected Component:** Wallet payment, transfer, recharge, and recurring transaction workflows / FR-002  |  **Category:** business_rules  |  **Severity:** high

**Description:** The BRD says users can link and manage wallets and perform fund transfers, bill payments, merchant purchases, recurring transactions, and wallet recharges. It does not define the eligibility rules, transaction limits, supported funding sources, approval conditions, balance sufficiency checks, fee handling, or how recurring schedules should behave. The phrase 'seamless transactions' describes the goal but not the rules that determine whether a transaction is valid or how it should be processed. This leaves critical business logic unspecified for core wallet transaction code.

**Missing / Under-specified:**
- transaction validation rules
- minimum and maximum transaction limits
- fee and charge calculation rules
- recurring schedule rules
- insufficient balance handling rules
- supported wallet funding sources

**Code Generation Impact:** The code generator cannot implement deterministic wallet transaction logic or validations without inventing banking rules.

**Assumption if unresolved:** The agent would assume basic validations such as positive amount, sufficient balance, daily limits, and fixed recurring intervals.

**Why this is a gap:** The BRD names transaction types but omits the conditions and calculations that govern them. Those rules are central to transaction processing and cannot be safely inferred for banking software.

## Gap 5: Currency rate source and update policy undefined

**Functional Area:** Enhance the online banking application to allow users to select both source and target currencies for conversion from a comprehensive and up-to-date list  |  **Affected Component:** Currency conversion service / FR-003  |  **Category:** interfaces_apis  |  **Severity:** high

**Description:** The BRD requires a comprehensive and regularly updated list of currencies with ISO 4217 codes and symbols, and says rates should be real-time or periodically updated from a reliable third-party API. However, it never identifies the third-party provider, refresh frequency, caching policy, base currency behavior, or whether conversions should use buy/sell/mid-market rates. It also does not specify whether historical conversion records must store the exact rate used at the time of conversion. These missing details prevent a Python agent from implementing a consistent and testable conversion engine.

**Missing / Under-specified:**
- exchange-rate provider
- rate refresh frequency
- rate type definition
- cache expiration policy
- historical rate retention rules
- base currency strategy

**Code Generation Impact:** The code generator cannot correctly fetch, cache, and apply exchange rates for currency conversion.

**Assumption if unresolved:** The agent would assume a public exchange-rate API with mid-market rates refreshed hourly and cached in memory.

**Why this is a gap:** The BRD explicitly references an external API but leaves all integration and rate-governance details open. Those choices materially affect correctness, persistence, reproducibility, and testability of conversion results.

## Gap 6: Security, audit logging, and failure behavior not measurable

**Functional Area:** Secure data handling and transaction mechanisms  |  **Affected Component:** Cross-cutting security controls for statement download, wallet integration, and currency conversion  |  **Category:** error_handling  |  **Severity:** high

**Description:** The BRD repeatedly states that downloads and transactions must be secure, reliable, compliant, and logged for audit, and mentions encrypted download links, access control validations, OAuth 2.0, and biometric verification. It does not specify encryption standards, token expiry, authorization rules by user role, required audit log fields, retention periods, timeout values, retry rules, or what errors should be returned on authentication, provider, or data failures. Because these controls affect every major workflow, the lack of measurable security and error-handling requirements makes implementation highly variable. An autonomous Python agent cannot produce compliant runtime behavior without inventing these operational rules.

**Missing / Under-specified:**
- authorization rules by role
- encrypted link expiry duration
- audit log field schema
- log retention period
- timeout and retry policy
- error response mapping

**Code Generation Impact:** The code generator cannot implement consistent secure failure paths, audit trails, or access-control enforcement across workflows.

**Assumption if unresolved:** The agent would assume standard JWT-based access control, 15-minute download link expiry, basic request/response audit logs, and generic retry behavior for transient errors.

**Why this is a gap:** The document states security outcomes but not the concrete implementation rules needed in code. In banking workflows, these are not safe defaults because they affect compliance, access control, and operational correctness.
