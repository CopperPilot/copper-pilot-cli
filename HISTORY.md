Changelog
=========


(unreleased)
------------
- Merge pull request #18 from CopperPilot/feat/acp-server. [Aditya]

  feat: add `copper-pilot acp` Agent Client Protocol server
- Fix(acp): surface hosted turn failures in the JSON-RPC error message.
  [AdityaNG, Claude Fable 5.1]

  T3 Code showed a bare "Internal error" for a prompt the hosted service
  had refused with "Upgrade to a paid plan to use Intelligence." because the
  explanation only lived in the error's `data`, which T3 does not display.
  Carry the hosted message in the error `message` (keeping the code), map a
  refused websocket handshake to auth_required when it is 401/403, log every
  failed turn to the diagnostics log, and never let an unexpected exception
  escape to the SDK's generic handler.
- Fix(acp): accept `copper-pilot acp auth login` for clients that append
  login args. [AdityaNG, Claude Fable 5.1]

  T3 Code runs an ACP terminal auth method by appending the method's args to
  the configured agent command, so the advertised `auth login` was spawned as
  `copper-pilot acp auth login`. The acp parser rejected the extra arguments
  with exit code 2 and T3 reported "The provider login command did not finish
  successfully." Normalize `acp auth ...` onto `auth ...` before parsing and
  document the alias.
- Feat: add `copper-pilot acp` Agent Client Protocol server. [AdityaNG,
  Claude Fable 5.1]

  Serve the hosted CopperPilot agent over ACP stdio so editors such as Zed
  can drive it with streaming messages, tool calls, permission prompts,
  session modes (ask/plan/agent), an effort config option, session/load
  resume of existing threads, and session/cancel.

  - New copper_acp.py built directly on the agent-client-protocol SDK;
    approvals and clarifying questions map onto session/request_permission.
  - `acp` optional extra; the command prints an install hint without it.
  - CI license gate ignores agent-client-protocol (Apache-2.0 LICENSE file,
    no license metadata in the wheel).
  - Tests: tests/test_acp.py (100% coverage of copper_acp.py, including a
    real stdio JSON-RPC round trip), parser and entrypoint tests, and a
    release-gate check for the extra.
  - Docs: cli.md, harness.md (Zed setup), README.


0.1.8 (2026-10-14)
------------------

Fix
~~~
- Annotate async context managers for ty 0.0.87. [AdityaNG]

  CI resolves the unpinned ty dependency to 0.0.87, which rejects AsyncIterator returns on asynccontextmanager.
- Upgrade the pinned Deep Agents runtime to 0.7.19. [AdityaNG]

  Keep the exact runtime pin while picking up the filesystem and media-handling fixes from 0.7.14.
- Align the CLI with hosted usage limits. [AdityaNG]

  Keep blocked turns machine-readable, shrink oversized resumed chats to the desktop frame budget, and report the installed client version.

Other
~~~~~
- Release: version 0.1.8 🚀 [AdityaNG]
- Merge pull request #16 from CopperPilot/update/14_oct_2026. [Aditya]

  Add Intelligence effort, hosted survey workers, and modal keyboard focus
- Fix(tests/test_tui.py) [AdityaNG]
- Feat(src/copper_pilot_cli/copper_status_events.py) [AdityaNG]
- Merge pull request #3 from
  CopperPilot/dependabot/github_actions/softprops/action-gh-release-3.
  [Aditya]

  build(deps): bump softprops/action-gh-release from 2 to 3
- Build(deps): bump softprops/action-gh-release from 2 to 3.
  [dependabot[bot]]

  Bumps [softprops/action-gh-release](https://github.com/softprops/action-gh-release) from 2 to 3.
  - [Release notes](https://github.com/softprops/action-gh-release/releases)
  - [Changelog](https://github.com/softprops/action-gh-release/blob/master/CHANGELOG.md)
  - [Commits](https://github.com/softprops/action-gh-release/compare/v2...v3)

  ---
  updated-dependencies:
  - dependency-name: softprops/action-gh-release
    dependency-version: '3'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #4 from CopperPilot/dependabot/pip/websockets-
  gte-15-and-lt-18. [Aditya]

  build(deps): update websockets requirement from <17,>=15 to >=15,<18
- Build(deps): update websockets requirement from <17,>=15 to >=15,<18.
  [dependabot[bot]]

  Updates the requirements on [websockets](https://github.com/python-websockets/websockets) to permit the latest version.
  - [Release notes](https://github.com/python-websockets/websockets/releases)
  - [Commits](https://github.com/python-websockets/websockets/compare/15.0...17.1)

  ---
  updated-dependencies:
  - dependency-name: websockets
    dependency-version: '17.1'
    dependency-type: direct:production
  ...
- Merge pull request #5 from CopperPilot/dependabot/pip/uuid-utils-
  gte-0.12-and-lt-2. [Aditya]

  build(deps): update uuid-utils requirement from <1,>=0.12 to >=0.12,<2
- Build(deps): update uuid-utils requirement from <1,>=0.12 to
  >=0.12,<2. [dependabot[bot]]

  Updates the requirements on [uuid-utils](https://github.com/aminalaee/uuid-utils) to permit the latest version.
  - [Release notes](https://github.com/aminalaee/uuid-utils/releases)
  - [Commits](https://github.com/aminalaee/uuid-utils/compare/0.12.0...1.0.0)

  ---
  updated-dependencies:
  - dependency-name: uuid-utils
    dependency-version: 1.0.0
    dependency-type: direct:production
  ...
- Merge pull request #2 from
  CopperPilot/dependabot/github_actions/actions/setup-python-7. [Aditya]

  build(deps): bump actions/setup-python from 5 to 7
- Build(deps): bump actions/setup-python from 5 to 7. [dependabot[bot]]

  Bumps [actions/setup-python](https://github.com/actions/setup-python) from 5 to 7.
  - [Release notes](https://github.com/actions/setup-python/releases)
  - [Commits](https://github.com/actions/setup-python/compare/v5...v7)

  ---
  updated-dependencies:
  - dependency-name: actions/setup-python
    dependency-version: '7'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #13 from
  CopperPilot/update/deepagents_0_7_19_3_oct. [Aditya]

  Upgrade the pinned Deep Agents runtime to 0.7.19
- Merge pull request #1 from
  CopperPilot/dependabot/github_actions/actions/checkout-7. [Aditya]

  build(deps): bump actions/checkout from 4 to 7
- Build(deps): bump actions/checkout from 4 to 7. [dependabot[bot]]

  Bumps [actions/checkout](https://github.com/actions/checkout) from 4 to 7.
  - [Release notes](https://github.com/actions/checkout/releases)
  - [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)
  - [Commits](https://github.com/actions/checkout/compare/v4...v7)

  ---
  updated-dependencies:
  - dependency-name: actions/checkout
    dependency-version: '7'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #12 from
  CopperPilot/update/contract_alignment_3_oct. [Aditya]

  Align the CLI with hosted usage limits


0.1.7 (2026-10-03)
------------------
- Release: version 0.1.7 🚀 [AdityaNG]
- Merge pull request #11 from
  CopperPilot/feat/python_versions_support_expansion. [Aditya]

  Support Python 3.11 and future releases
- Ci: probe Python 3.15 weekly instead of on pull requests. [AdityaNG]

  Keep the upstream install failure visible without turning required pull request checks red.
- Feat(py11) [AdityaNG]
- Merge pull request #7 from CopperPilot/feat/example_002_cm5_camera.
  [Aditya]

  feat(examples/002_cm5_camera): add 3D pose audit and generated carrier
- Fix(examples/002_cm5_camera): render the README PCB as a dimetric 3D
  view. [AdityaNG, Cursor]

  Use an orthogonal high-quality camera with a slight isometric tilt so the
  hero PNG shows connector height instead of a flat top-down shot.
- Fix(examples/002_cm5_camera): skip library dirs relative to the
  workspace. [AdityaNG, Cursor]

  Absolute path parts named tmp (Linux /tmp pytest roots) were skipping the
  entire haystack, so libraries_ready failed on Ubuntu CI.
- Feat(examples/002_cm5_camera): add 3D pose audit and generated
  carrier. [AdityaNG, Cursor]

  Lock connector 3D offsets, Module1 pad orientation, and the generated
  KiCad project so the example reproduces NANO-C assembly parity.
- Feat(examples/002_cm5_camera/main.py): new example. [AdityaNG]


0.1.6 (2026-09-18)
------------------
- Release: version 0.1.6 🚀 [AdityaNG]
- Merge pull request #6 from CopperPilot/feat/plugins. [Aditya]

  feat: add MCP conductor and Claude Code plugin
- Clarify server restrictions in PR template. [Aditya]

  Updated checklist item to clarify server restrictions.
- Feat: add MCP conductor and Claude Code plugin. [AdityaNG, Cursor]

  Let Claude Code, Cursor, and Codex delegate electronics work to the hosted agent via copper-pilot mcp, a portable review skill, and an in-repo plugin.


0.1.5 (2026-09-17)
------------------
- Release: version 0.1.5 🚀 [AdityaNG]
- Fix(README.md) [AdityaNG]


0.1.4 (2026-09-17)
------------------
- Release: version 0.1.4 🚀 [AdityaNG]
- Fix(tests/test_release_gates.py) [AdityaNG]


0.1.3 (2026-09-17)
------------------
- Release: version 0.1.3 🚀 [AdityaNG]
- Docs. [AdityaNG]


0.1.2 (2026-09-17)
------------------
- Release: version 0.1.2 🚀 [AdityaNG]
- Ci(pyproject.toml) [AdityaNG]
- Fix(tests/test_tui.py) [AdityaNG]


0.1.1 (2026-09-17)
------------------
- Release: version 0.1.1 🚀 [AdityaNG]


0.1.0 (2026-09-17)
------------------
- Release: version 0.1.0 🚀 [AdityaNG]
- Fix(tests/test_release_gates.py) [AdityaNG]
- Test. [AdityaNG]
- Docs. [AdityaNG]
- Docs. [AdityaNG]
- Example(examples/001_esp32/) [AdityaNG]
- Makefile. [AdityaNG]
- Ci. [AdityaNG]
- Docs. [AdityaNG]
- Refactor(name) [AdityaNG]
- Init. [AdityaNG]


