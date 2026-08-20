# A0 ACP Registry Admission Plan

## Goal

Make the A0 connector ready for ACP Registry admission by advertising and
executing a real ACP terminal-authentication flow, then prepare a release
artifact that can be installed as the `a0` PyPI distribution.

## Scope and decisions

- Use the existing Agent Zero browser-session cookie persistence mechanism.
- Add `a0 acp --login` as the terminal-auth command advertised to ACP clients.
- Do not persist usernames or passwords; prompt only in the terminal-auth
  process and persist a session only after successful verification.
- Advertise terminal authentication only when the ACP client declares support.
- Prepare PyPI publication through a release workflow. Actual publication and
  registry submission depend on the upstream A0 release being merged/tagged
  and the PyPI project/trusted publisher being configured.

## Step-by-step tasks

1. Update `src/agent_zero_cli/acp.py` to authenticate with an existing session
   or terminal-prompted credentials, persist only a verified session cookie,
   and conditionally include the ACP terminal auth descriptor.
   - Validation: run the focused ACP tests.

2. Update `src/agent_zero_cli/__main__.py` so `a0 acp --login` reaches the
   terminal-auth flow without starting the stdio ACP server.
   - Validation: run focused entrypoint and ACP tests.

3. Add focused tests in `tests/test_acp.py` and `tests/test_entrypoint.py` for
   terminal capability negotiation, saved-session reuse, successful login,
   failed login, and CLI routing.
   - Validation: run the two changed test files.

4. Bump the package version, document the ACP login/install contract in
   `README.md`, and add a GitHub release-to-PyPI trusted-publishing workflow.
   - Validation: package build and focused tests.

## Final validation

1. Run `python -m pytest tests/test_acp.py tests/test_entrypoint.py -v` with
   the project environment.
2. Build the distribution and inspect its metadata/version.
3. Run the full suite if a project environment is available without installing
   dependencies.

## Out of scope

- Changing ACP Registry schema/runtime support for `uvx --from`; that is
  tracked independently upstream as agentclientprotocol/registry#296.
- Publishing to PyPI, tagging an upstream release, or merging a PR before the
  repository maintainers approve the source change.
