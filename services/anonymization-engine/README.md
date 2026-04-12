# Anonymization Engine

This service performs pre-provider masking of sensitive request data and optional response de-anonymization.

Responsibilities:

- deterministic masking of structured sensitive data
- local heuristic NER-style masking for people, organizations, and addresses
- request-scoped placeholder generation
- response de-anonymization after upstream model invocation
