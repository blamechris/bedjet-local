# ADR-0003 — MIT repo; upstream used as evidence, never as source

**Date:** 2026-08-16
**Status:** Accepted

## Context

The brief says to reuse existing open-source work where appropriate, understand its licensing,
and attribute it properly. The survey established what is actually available:

- **ESPHome's BedJet codec is GPLv3.** ESPHome is dual-licensed: `.c/.cpp/.h/.hpp/.tcc/.ino`
  are GPLv3, Python and everything else is MIT. `bedjet_codec.{h,cpp}` and `bedjet_hub.{h,cpp}`
  — the parts that matter — fall on the GPLv3 side.
- **`markus1189/bedjet-re` carries no licence at all**, i.e. all rights reserved. Its content
  is additionally derived from decompiling the vendor's Android app.
- The MIT-licensed HA custom integrations (`asheliahut` — archived, `robert-friedland` —
  dormant, `blueharford` — current) are permissively licensed and *may* be reused directly.

## Decision

**This repository is MIT.** Upstream GPLv3 and unlicensed work is used as **technical
evidence** — a source of hypotheses to verify — and never copied, transliterated, or ported.

The line we are drawing, explicitly:

| Allowed | Not allowed |
|---|---|
| "The status characteristic is `00002000-bed0-…`" | Copying `bedjet_codec.cpp`'s parsing function and renaming it |
| "Mode `0x03` is heat" | Reproducing upstream's enum block verbatim |
| "Fan percent is derived from a 0–19 step index" | Transliterating upstream's conversion, comments and all |
| Reading upstream to decide *what to test* | Reading upstream to decide *what to type* |

Protocol facts — a UUID, a byte offset, a numeric mapping — are functional facts, not
copyrightable expression. A structure-for-structure translation of someone's parser is a
derivative work regardless of the language it lands in. We stay clearly on the correct side of
that line rather than close to it.

Practically: our decoder is written from the *field table* in
[`PROTOCOL.md`](../protocol/PROTOCOL.md), and every field there carries a provenance tag and a
confidence level. Once we have our own captures, our fixtures become the authority and
upstream becomes a footnote.

**MIT-licensed upstreams may be used directly** where genuinely useful, with attribution in
`NOTICE` and an inline comment. We currently have no need to — the protocol is simple enough
that a clean implementation is cheaper than a compliance audit.

## Attribution

`NOTICE` credits the ESPHome project, the HA custom-integration authors, and `bedjet-re` for
the protocol knowledge this project builds on. Attribution is owed for the *research* even
where no code is taken, and it is cheap to give.

## Consequences

- No `git submodule`, no vendored upstream, no `pip install` of a GPL BedJet library.
- If we ever *do* want to depend on GPLv3 code, that is a relicensing decision for the whole
  repo and needs a new ADR — not a quiet import.
- Anyone reviewing `src/protocol/` should be able to trace each constant to a `PROTOCOL.md`
  row, not to an upstream file.
