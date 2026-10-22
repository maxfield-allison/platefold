<!--
Thanks for contributing to Plate|Fold. Keep the change focused and give reviewers enough evidence to understand what changed and what remains untested.
-->

## Outcome

<!-- What changes, who notices, and why does it matter? For maintenance work, describe the maintainer or release consequence. -->

## Related issue

<!-- Link the issue or decision this change implements. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation
- [ ] Maintenance, refactor, build, or CI

## Validation

<!-- Keep only the checks that apply and describe any skipped or unavailable evidence. -->

- [ ] Ruff, nesting, formatting, and Pyright checks pass
- [ ] `python -m pytest -m "not integration"` passes
- [ ] Browser integration tests pass when rendering behavior changed
- [ ] `npm run build` passes in `desktop` when the desktop interface changed
- [ ] Representative PDFs were inspected when layout, fonts, diagrams, or pagination changed
- [ ] A clean-machine or installed-artifact check was run when packaging changed
- [ ] Documentation and `CHANGELOG.md` were updated for user-visible behavior

## Limits and follow-up

<!--
A green test suite or successful build doesn't show that the change works on every operating system, architecture, browser, package, or signing path. State what wasn't tested and link any follow-up work.
-->
