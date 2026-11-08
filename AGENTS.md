# LanPilot / 邻控 for agents

Use LanPilot when the user asks to see or operate a paired Windows desktop from macOS, control several PCs concurrently, or perform supported file jobs locally with screen verification.

- If a LanPilot MCP connection already exists, call `list_devices` and select the intended device. For a new stdio connection, use `python3` with the absolute path to [lanpilot_mcp.py](lanpilot_mcp.py) as the argument, in the configured controller account. Starting the MCP process does not enroll a Windows device.
- Inspect the screen with `observe`. Use that device's returned frame ID for `act`; then verify the resulting screen. On an uncertain result, `recover` the original request ID. Independent devices can work concurrently; preserve action order on each device.
- For supported CSV workflows, use [local jobs](docs/local-jobs.md). The worker does not offer arbitrary remote shell execution. A successful worker receipt is not proof that a business website accepted a submission.
- For a new computer, use [private invitations](docs/invitations.md). Never publish an invitation, device token or real screenshot. For operating instructions see the [LanPilot skill](skill/lanpilot/SKILL.md).
