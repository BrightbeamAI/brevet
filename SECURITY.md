# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to
**oss@brightbeam.com**. Do not open a public issue for
security reports. You will receive an acknowledgement within five
working days.

Reports of particular interest, given what Brevet protects:

- any way to raise a capability's authority without a recorded human
  or mission-group identity (invariant I2, the second authority
  invariant in `SPEC.md` section 2)
- Evidence-layer material resolving into a lockfile (invariant I1), or
  recalled material reaching a lockfile or a running agent (`SPEC.md`
  section 7)
- a decision, release or register change accepted without the approver
  signatures the workspace requires, including a change to a mission
  group made without that group's quorum
- evidence-chain manipulation that `brevet verify` fails to detect,
  including a chain cut short, rewritten or replaced behind its anchors
- an agent running, outside the shadow channel, anything other than its
  latest signed release: a modified manifest, lock or harness that still
  verifies
- a tool call that escapes the tool tiers a release declares

## Supported versions

| Version | Supported |
|---|---|
| 0.4.x | yes |
| 0.3.x and earlier | no: upgrade to 0.4 |

## Scope notes

The rules a conforming deployment enforces are in `SPEC.md`; how each
control is switched on is in `ABOUT.md` under "Running in production".
Approver keys live outside the workspace, encrypted with a passphrase or
held as the approver's own SSH key, and can be checked against an
allowed-signers file or GitHub. The evidence chain's head is anchored
outside the workspace, in a file the agent cannot write or a CHAP
coordinator's audit log. Brevet assumes an attacker who controls the
workspace does not also hold an approver's unlocked key or write access
to every anchor target.
