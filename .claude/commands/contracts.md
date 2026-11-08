---
description: Show the module ownership table and the public API contract for a module
argument-hint: [module-name, e.g. solar | providers | route | energy | vehicle]
---

Read `docs/CONTRACTS.md` and print:

1. The ownership row for `$ARGUMENTS` (paths the module owns exclusively).
2. The required public API block for `rwi.$ARGUMENTS`, verbatim.
3. The hard requirements listed under that module.

If `$ARGUMENTS` is empty, print the full ownership table from section 2
instead. Do not summarise or paraphrase the API signatures — they are
binding.
