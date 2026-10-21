# Wide Diagram Fixture

Document: Placement ladder exercise
Version: 2.1.0
Owner: Test Suite

---

# 1. Narrow diagram

This one is small enough to sit in the running text column.

```mermaid
flowchart LR
    a["Start"] --> b["End"]
```

# 2. Wide diagram

Wide enough that scaling it into the text column would make it unreadable, so
it must be promoted onto a page of its own.

```mermaid
flowchart LR
    a1["Regional ingest gateway with a deliberately long label"] --> b1["Schema validation and type coercion stage"]
    b1 --> c1["Canonical model transformation with enrichment"]
    c1 --> d1["Fan-out publication to downstream consumers"]
    d1 --> e1["Cold storage archive for replay and audit"]
    e1 --> f1["Reconciliation and completeness verification"]
    f1 --> g1["Operator notification and dashboard refresh"]
```

# 3. Fenced block that merely mentions mermaid

The block below documents the syntax. It must not be rendered as a diagram.

````markdown
```mermaid
flowchart TB
    this --> should_not_render
```
````

Text after the nested fence.
