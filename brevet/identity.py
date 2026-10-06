"""Who may take a decision.

Every dawn decision, release and recall names the identity that took it.
Only ``human:<who>`` and ``mission_group:<name>`` identities are accepted;
``agent:``, ``model:``, ``dream:`` and every other namespace are refused.
This module checks the form of an identity. Proving that the named person
took the decision is the job of signed approvals (``brevet.approvals``).
"""

from __future__ import annotations

import re

IDENTITY = re.compile(r"^(human|mission_group):[^\s:]\S*$")


def require_identity(identity: str | None, *, role: str = "approver",
                     mission_group: bool = False) -> str:
    """Return the identity, stripped, if it names a human or a mission group.

    Refuses empty identities, machine namespaces (agent:, model:, dream:)
    and anything not written ``human:<who>`` or ``mission_group:<name>``.
    With ``mission_group=True`` only a mission group is accepted."""
    who = (identity or "").strip()
    if not IDENTITY.match(who):
        raise PermissionError(
            f"the {role} must be a human or a mission group, written human:<who> or "
            f"mission_group:<name>; got {identity!r}. Machine identities such as "
            f"agent:, model: and dream: cannot take this decision.")
    if mission_group and not who.startswith("mission_group:"):
        raise PermissionError(
            f"the {role} must be a mission group (mission_group:<name>) for this "
            f"decision; got {who!r}")
    return who
