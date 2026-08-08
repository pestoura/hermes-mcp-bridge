# Capability Projection

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Hermes may contain hundreds of internal tools and skills. The external client must receive only a policy-approved subset.

```text
Hermes Internal Registry
        |
        v
policy / allowlist / principal / scope
        |
        v
Projected Tool Surface
        |
        v
ChatGPT / MCP client
```

Projection must exclude secrets, credential values, secret paths and unrestricted dangerous capabilities. Schemas should include only fields required by the client contract.

## Projection inputs

- principal/tenant context;
- resource scope;
- active policy version;
- tool security tier;
- credential capability availability (not secret values);
- capability health;
- environment/stability constraints;
- protocol/schema negotiation.

## Open design choice

Static projection is simpler and auditable; dynamic projection can reduce context/tool count further. V2 must document and test whichever model is selected and must avoid metadata supplied by an untrusted backend silently expanding authority.

## Internal MCP proxying

An internal MCP server such as Home Assistant may be projected through normalized typed tools, but internal MCP metadata is not trusted automatically. Tool names, schemas, risk classification and policy actions must be normalized and independently governed by the canonical registry.
