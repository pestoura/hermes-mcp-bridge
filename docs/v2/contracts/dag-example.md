# DAG Contract Example

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

User intent: find who opened issue 123 and search email from that person.

```yaml
schema_version: v2-draft
mode: DAG
operations:
  - id: issue
    tool: github.get_issue
    args:
      repository: owner/project
      number: 123
  - id: emails
    tool: email.search
    depends_on: [issue]
    bindings:
      args.query:
        from: issue.result.author.email
        type: email_address
budget:
  max_nodes: 2
  max_parallelism: 2
```

The binding engine validates declared source/target types. It does not use arbitrary eval, shell interpolation or executable templates. Cycles, missing outputs and ambiguous bindings fail before unsafe execution.
