<!-- Keep PRs small and focused. Thanks for contributing to QueryHub. -->

## What & why

<!-- What does this PR change, and why? Link any related issue: Fixes #123 -->

## Type

- [ ] Bug fix
- [ ] Feature
- [ ] Refactor / cleanup
- [ ] Docs
- [ ] Security

## Checklist

- [ ] `ruff check src tests scripts` passes
- [ ] `pytest` passes
- [ ] Added or updated tests for the change
- [ ] No secrets, real hostnames, account IDs, or personal data in the diff
- [ ] Updated the docs (README / docs/) if behavior changed
- [ ] Checked the highest `migrations/NNN_*.sql` number on `main`, if this PR adds a migration
- [ ] Wrote a What's-new draft below, if users see the change

## Security note

<!-- Does this PR affect the trust chain: query safety, tier classification,
     credentials, approval, masking, or audit? If yes, describe the impact
     and how you verified it. Do NOT post exploit details in a public PR.
     For private disclosure, see SECURITY.md. -->

## What's-new draft

<!-- For a change that users see: one or two short sentences. The maintainer writes the final entry. Delete this section if users do not see the change. -->
