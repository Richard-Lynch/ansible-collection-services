# SPDX-License-Identifier: GPL-3.0-or-later
"""Test double for richard_lynch.common.onepassword_cached: returns a fixed fake token, never calls `op`.

Molecule puts this collection path FIRST, so the role resolves the token through exactly the
lookup call it makes in production, and the Slack stub server rejects any other bearer.
"""

from __future__ import annotations

from ansible.plugins.lookup import LookupBase

DOCUMENTATION = """
name: onepassword_cached
short_description: molecule stub of the 1Password cached lookup
description: Returns stub-token-<field> for any item; asserts the role passed field and vault.
options:
  _terms: {description: item, required: true}
  field: {description: field, required: true}
  vault: {description: vault, required: true}
  section: {description: section, required: false}
"""


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)
        if not self.get_option("vault"):
            raise ValueError("stub onepassword_cached: the role passed no vault")
        if terms and terms[0] == "empty-item":
            return [""]
        return [f"stub-token-{self.get_option('field')}" for dummy in terms]
