# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to
**arsalan.shahid@brightbeam.com**. Do not open a public issue for
security reports. You will receive an acknowledgement within five
working days.

Reports of particular interest, given what Brevet protects:

- any way to raise a capability's authority without a recorded human
  or mission-group identity (invariant I2)
- Evidence-layer or recalled material resolving into a lockfile
  (invariants I1 and I5)
- evidence-chain manipulation that `brevet verify` fails to detect
- signature bypass: executing a modified manifest that still verifies

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Scope notes

Brevet's threat model is documented in the technical report and in
`SPEC.md`. Signing uses per-workspace Ed25519 keys without rotation or
transparency-log anchoring in this release; deployments needing
hardened identity should follow the CHAP profile ecosystem.
