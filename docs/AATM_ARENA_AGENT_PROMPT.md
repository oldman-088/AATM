Repository: oldman-088/AATM, branch main (v3.1.0 or later).

Work on ONE branch with ONE pull request. Push after every implementation item. Do not merge the PR yourself.

Read the actual source code before making any changes. Do not assume how the existing system works.

Required files to inspect:
```bash
backend/app/browser/arena_connector.py
bridge/connector.py
bridge/server.py
backend/app/providers/adapters/arena_agent.py
arena_browser.py
backend/app/api/routes/arena.py
backend/app/core/config.py
backend/app/orchestration/*
backend/app/git_workflows/*
backend/app/github_client/*
frontend/src/views/ArenaViews.tsx
autoscaler
```
The arena_agent.py adapter is currently marked as "not implemented".

If another branch (such as the Command Bridge UI) is already open, merge the latest main into your working branch before opening the PR. Preserve both features and all existing functionality.
Make every AATM worker capable of driving an independent Arena Agent session through a Tampermonkey userscript running inside that worker's own Chrome profile.

Arena Agent is the web-based coding agent that works directly with GitHub, creates its own arena/<id> branches, modifies code, pushes commits, and opens Pull Requests.

The goal is to fully automate the workflow previously performed manually by the project owner.

AATM should become a central management and orchestration system for multiple Arena Agent sessions.

The complete workflow should include:
1. The manager receives a development directive.
2. The manager decomposes it into individual tasks.
3. The manager generates a complete, detailed prompt for each task.
4. AATM starts or reuses an Arena Agent session for the assigned employee.
5. AATM submits the task prompt.
6. AATM continuously monitors the Arena Agent session.
7. AATM handles Keep Working interactions and follow-up messages.
8. AATM detects resulting branches and Pull Requests through GitHub.
9. AATM monitors code changes and commits.
10. AATM retrieves the agent's final report.
11. A reviewer employee independently verifies the report against the actual GitHub diff.
12. AATM requests corrections when necessary.
13. The owner reviews and approves the final changes through the existing Approval Center.
14. Changes are merged only after owner approval.
The system should support all available employee profiles and Arena Agent sessions without imposing artificial profile-count, submission-count, or usage-count restrictions.
Remove the temporary authorization system and all associated approval-expiration restrictions.

Do not implement:
- Temporary authorization references.
- Authorization start or expiration dates.
- Authorization renewal requirements.
- Profile-count restrictions.
- Submission-count restrictions.
- Global in-flight submission restrictions.
- Daily or hourly usage quotas.
- Artificial request intervals.
- Usage-based automatic pauses.
- Maximum employee or Arena profile assignments.
Remove all environment variables associated with these restrictions.

The Arena integration should be controlled by the actual connector configuration and operational availability.

Keep only configuration settings that are genuinely necessary for operating the integration.

The owner should be able to configure:
```ini
ARENA_CONNECTOR_ENABLED=true
ARENA_ALLOWED_ORIGINS=https://arena.ai
ARENA_AGENT_NEW_SESSION_URL=
ARENA_AGENT_MAX_KEEP_WORKING=0
```
For ARENA_AGENT_MAX_KEEP_WORKING, support unlimited continuation when configured as 0.

Do not introduce hidden usage quotas or artificial limits elsewhere in the code.

All employees should be eligible for Arena Agent mode when their configuration and available resources permit it.

The autoscaler should be able to assign Arena Agent mode without an arbitrary maximum-profile restriction.

The system should dynamically manage multiple Chrome profiles and sessions.
Reference project:

https://github.com/anuragkumarnishad/arena-auto-continue

License: MIT.

Clone the repository and thoroughly inspect its complete implementation, especially:
```
arena-auto-continue.user.js
```
Use the reference project as the primary implementation foundation.

Reuse, adapt, and integrate its available functionality into AATM rather than implementing a reduced version.

Study the complete source code and identify all useful capabilities, including:
- Page element detection.
- Composer detection.
- Input handling.
- Send-button detection.
- Stop-button detection.
- Keep Working detection.
- Automatic continuation functionality.
- Session monitoring.
- Agent response detection.
- DOM observation.
- Page-state monitoring.
- Background-tab considerations.
- Error handling.
- Human verification detection.
- Session recovery.
- Input interaction.
- React-controlled input handling.
- Any additional useful automation functionality.
Preserve the reference project's useful functionality and adapt its architecture to AATM's Bridge, backend, employee orchestration, and frontend.

Do not arbitrarily remove useful functionality from the reference implementation.

Include the original MIT license notice and attribution in:
```
THIRD_PARTY_NOTICES.md
```
Also add an attribution comment to the adapted userscript.

Ensure the integration remains maintainable and compatible with future updates to the Arena interface.
Implement the following items as separate commits.
Implement complete Arena integration.

Requirements:
- Remove the temporary approval system.
- Remove approval-expiration checks.
- Remove profile-count restrictions.
- Remove artificial submission restrictions.
- Remove daily and hourly usage quotas.
- Remove artificial request intervals.
- Remove usage-based back-off restrictions.
- Support all available employee profiles.
- Support multiple independent Arena sessions.
- Allow the autoscaler to assign Arena Agent mode dynamically.
- Maintain accurate session ownership and employee associations.
- Maintain reliable session state tracking.
- Preserve operational logs and error reporting.
- Support recovery after browser disconnections and temporary failures.
The system should dynamically manage all available Arena workers without artificial usage restrictions.
Upgrade the generated userscript through:
```
backend/app/browser/arena_connector.py
```
Generate an independent userscript for every employee Chrome profile.

Userscript requirements:
- Generate the correct @match directives from ARENA_ALLOWED_ORIGINS.
- Include the local mock Arena URL.
- Use @grant none.
- Use the existing per-profile hashed token mechanism for Bridge authentication.
- Never expose backend secrets or GitHub credentials to the userscript.
- Maintain communication with the local Bridge through WebSocket.
- Reuse the complete useful functionality of the reference implementation.
Implement the following commands.

1. new_session
- Navigate to ARENA_AGENT_NEW_SESSION_URL.
- Alternatively, locate and activate the New button using the reference project's element-detection techniques.
- Wait until the composer is available.
- Report ready when the session is ready.
2. submit {text}
- Locate the appropriate input.
- Insert the supplied task prompt.
- Trigger the required input events.
- Locate and activate the Send button.
- Monitor generation.
- Report generating while the Stop button is visible.
- Report idle after generation has stopped for five seconds.
3. keep_working
- Detect the Keep Working button.
- Activate it when instructed by the Bridge.
- Monitor the resulting generation.
- Report state changes.
4. read_last_reply
- Locate and retrieve the latest Arena Agent response.
- Return the response to the backend.
- Apply appropriate size limits and sensitive-information redaction in the backend.
5. selector_test
- Inspect the Arena page.
- Detect the composer.
- Detect the Send button.
- Detect the Stop button.
- Detect the Keep Working button.
- Detect the latest agent response.
- Detect human verification elements.
- Return a detailed selector report.
The selector test must not send messages or perform unintended actions.
Support:
```
ready
generating
idle
keep_working_visible
needs_human
offline
```
Use DOM observation and the reference project's monitoring techniques to detect state changes.

All operations should be coordinated through the Bridge so that AATM maintains control and visibility over employee sessions.
Implement:
```swift
backend/app/providers/adapters/arena_agent.py
```
Create a fully functional ArenaAgentAdapter that integrates Arena Agent as an employee execution engine.
The manager must generate a comprehensive prompt for each assigned task.

Each prompt must include:
- Repository.
- Base branch: main.
- Detailed task description.
- Expected implementation.
- Acceptance criteria.
- Relevant project architecture.
- Existing coding conventions.
- Testing requirements.
- Security requirements.
- Instructions not to implement fake or nonfunctional features.
- Instructions to preserve existing functionality.
Every task prompt must include these GitHub workflow requirements:
```sql
Work on ONE branch.

Create ONE pull request.

Push changes after every completed implementation item.

Do NOT merge the pull request yourself.

Put [AATM-<task_id>] at the beginning of the pull request title.
```
Implement the complete session workflow:
```yaml
new_session
     |
     v
submit task prompt
     |
     v
monitor Arena session
     |
     v
detect Keep Working
     |
     v
continue the session
     |
     v
monitor implementation
     |
     v
detect completion
```
The adapter must support:
- Starting new sessions.
- Submitting task prompts.
- Monitoring agent generation.
- Continuing paused tasks.
- Sending follow-up messages.
- Recovering disconnected sessions.
- Detecting completed work.
- Retrieving final responses.
Keep Working continuation should support unlimited operation when configured accordingly.
Pull Request detection must use GitHub API rather than relying on the Arena webpage.

Search for open Pull Requests whose titles begin with:
```
[AATM-<task_id>]
```
Also monitor newly created branches matching:
```
arena/*
```
When a matching Pull Request is discovered:
- Associate it with the correct task.
- Associate its branch with the employee.
- Store the Pull Request URL.
- Store the branch name.
- Track subsequent commits.
- Update the task status to:
```
pr_open
```
When the Arena session becomes idle and a corresponding Pull Request exists:

Execute:
```
read_last_reply
```
Store the returned content as the agent's task report.

The report should include:
- Implementation summary.
- Modified files.
- Completed requirements.
- Tests performed.
- Test results.
- Known limitations.
- Outstanding work.
Assign a reviewer employee to independently inspect the actual GitHub changes.

The reviewer must compare the agent's report against:
- Actual modified files.
- Commit history.
- GitHub diff.
- Implemented functionality.
- Test coverage.
- Test results.
- Acceptance criteria.
Do not trust the agent's report without verifying the actual code.

If discrepancies are discovered:
- Generate a detailed correction prompt.
- Send it to the same Arena session through a follow-up message.
- Monitor the resulting changes.
- Retrieve an updated report.
- Repeat verification as necessary.
If the issue cannot be resolved, escalate it to the owner.
Preserve the existing Approval Center and approval-gated GitHub merge workflow.

Arena Agent must never merge its own Pull Request.

The owner must approve the changes before merging.

Use the existing squash merge workflow:
```sql
gh pr merge --squash
```
Only execute the merge after the required owner approval has been recorded.
Arena may lock a branch after its Pull Request is merged.

Therefore, the next task assigned to that employee must start with:
```
new_session
```
Do not assume that a previously merged session can be reused.
Monitor session activity continuously.

Detect:
- Stalled sessions.
- Browser disconnections.
- Failed submissions.
- Missing responses.
- Interrupted generation.
- Unexpected page states.
- Human verification requirements.
If no meaningful session-state change occurs for 20 minutes, flag the session as:
```
needs_human
```
Notify the owner or responsible operator.

If a task cannot produce a Pull Request despite continued execution and recovery attempts, escalate it rather than reporting false completion.
For every active Arena task, monitor its corresponding:
```
arena/*
```
branch through GitHub API.

Track:
- New commits.
- Changed files.
- Added code.
- Removed code.
- Modified code.
- Differences against the base branch.
- Implementation progress.
Publish changes through the existing Command Bridge WebSocket event:
```
agent.code
```
Ensure that streamed code is:
- Redacted where necessary.
- Properly size-limited for transport.
- Throttled to avoid unnecessary repeated updates.
- Associated with the correct employee and task.
- Associated with the corresponding GitHub branch.
If the Command Bridge UI has not yet been merged, expose the events and create a simple live-diff panel in the Arena view.
Extend:
```
frontend/src/views/ArenaViews.tsx
```
Create a comprehensive Arabic-first Arena management dashboard.

The dashboard should display:
- Arena connector status.
- Number of active employee profiles.
- Number of active Arena sessions.
- Total running tasks.
- Completed tasks.
- Failed tasks.
- Current session activity.
For each employee, display:
- Employee name or identifier.
- Chrome profile.
- Arena session status.
- Current task.
- Current execution state.
- Current GitHub branch.
- Pull Request URL.
- Last activity.
- Keep Working activity.
- Error information.
- Human intervention events.
Display the current task distribution and execution status.

Do not impose artificial submission-count, usage-count, or profile-count restrictions.

The system should support dynamic employee allocation and session management.
Provide a live-diff panel showing:
- Recently modified files.
- New commits.
- Added lines.
- Removed lines.
- Latest changes.
- GitHub comparison links.
Provide the following controls:
- Start New Session.
- Submit Task.
- Continue Working.
- Read Last Reply.
- Selector Test.
- Generate Userscript.
- Rotate Userscript.
- Resume Session.
- View Live Diff.
- View Pull Request.
Display clear notifications when a session requires human attention.

Provide a Resume control after the human issue has been resolved.

The interface should be Arabic-first, responsive, and consistent with the existing AATM design system.
Update:
```
docs/OPERATIONS.md
```
Add a section titled:

وضع وكيل Arena (التشغيل الكامل)

Document the complete setup and operational workflow.

Include:
Explain how to create independent Chrome profiles for employees.

Support all available profiles without an arbitrary four-profile restriction.
For each profile:
- Sign in to Arena manually.
- Connect the appropriate GitHub account manually.
- Verify that the account can access the target repository.
Explain the Chrome Memory Saver setting.

Recommend disabling Memory Saver for Arena profiles and keeping their Chrome windows open and active.

Explain that Chrome may throttle or suspend background tabs.
Explain how to:
- Install Tampermonkey.
- Generate the employee-specific userscript.
- Install the userscript.
- Update or rotate the userscript when necessary.
Document all required .env variables.

Explain which settings are optional and which are required.

Do not document removed authorization, profile-count, submission-count, or usage-quota settings.
Explain how to run Selector Test and inspect its results.

If the real Arena interface differs from the implemented heuristics and the selector test fails, stop and ask the owner to inspect the page rather than guessing.
Recommend starting with one profile and one small task to validate the integration.

After successful validation, progressively activate additional employee profiles.

This is a recommended rollout procedure, not a permanent profile restriction.
The system must stop the affected session and request manual intervention.

After the human resolves the issue, the operator can resume the session.
Document:
- Browser disconnections.
- Userscript communication failures.
- Missing selectors.
- Session recovery.
- Failed submissions.
- GitHub authentication problems.
- Pull Request detection problems.
- Stalled Arena sessions.
All new functionality must have meaningful automated tests.
Verify:
- Arena connector activation.
- Multiple employee profiles.
- Dynamic autoscaler assignment.
- Independent session management.
- Session recovery.
- Bridge communication.
- GitHub integration.
- Task-to-session association.
Ensure that no removed profile-count or usage-quota restrictions remain in the implementation.
Extend:
```
bridge/mock_arena/
```
Create a mock Arena Agent page containing:
- A message composer.
- A Send button.
- A Stop button during generation.
- A Keep Working button appearing during execution.
- A final agent response.
- A CAPTCHA variant.
- A simulated login wall.
- A simulated disconnected session.
Test:
Function	Expected result
new_session	Creates or navigates to a new session
submit	Inserts and sends the supplied prompt
keep_working	Continues the current task
read_last_reply	Retrieves the latest agent response
selector_test	Detects available elements without sending
CAPTCHA detection	Reports needs_human
Offline detection	Reports offline
Session recovery	Restores monitoring when possible
Also test the useful automation functionality adapted from the reference project.
Test the complete workflow:
```yaml
Create task
    |
    v
Generate task prompt
    |
    v
Verify [AATM-<task_id>] requirement
    |
    v
Start Arena session
    |
    v
Submit prompt
    |
    v
Monitor execution
    |
    v
Detect GitHub branch
    |
    v
Detect Pull Request
    |
    v
Associate PR with task
    |
    v
Retrieve agent report
    |
    v
Reviewer verifies actual code
    |
    v
Detect discrepancies
    |
    v
Send correction prompt
    |
    v
Verify updated implementation
    |
    v
Request owner approval
    |
    v
Merge only after approval
    |
    v
Start next task using new_session
```
Verify:
- Backend credentials are never exposed to the userscript.
- GitHub credentials are not transmitted through the userscript.
- Bridge authentication works correctly.
- Sensitive information is redacted from reports and live diffs.
- Human verification is detected correctly.
- Sessions stop when human intervention is required.
- No unauthorized GitHub merge can occur.
- Userscript commands are correctly validated.
- Multiple employee sessions do not interfere with each other.
Verify that:
```
THIRD_PARTY_NOTICES.md
```
contains the complete applicable MIT license notice and original project attribution.
After implementation, provide a complete report.
Provide the actual GitHub Pull Request URL.

Do not merge it.
Provide a table:
Requirement	Tests	Commit
R1 — Full Integration	Integration tests	Commit SHA
R2 — Userscript	Userscript tests	Commit SHA
R3 — Arena Agent Adapter	Workflow tests	Commit SHA
R4 — Live Code	WebSocket and GitHub tests	Commit SHA
R5 — Arena Dashboard	Frontend tests	Commit SHA
R6 — Operations Guide	Documentation review	Commit SHA
Each implementation item must have its own commit.

Push after every completed item.
Provide the exact .env lines required by the owner.

Include only settings that are actually used by the implementation.
Provide a practical setup checklist covering:
- Chrome profile creation.
- Arena login.
- GitHub account connection.
- Chrome configuration.
- Tampermonkey installation.
- Userscript generation.
- Userscript installation.
- Environment configuration.
- Selector testing.
- Initial task execution.
- Additional employee activation.
Clearly identify anything that was not implemented, partially implemented, or requires manual configuration.

Never claim that a feature is complete unless it has actually been implemented and tested.
1. Read the real repository code before implementing anything.
2. Reuse the existing AATM architecture wherever possible.
3. Reuse the full useful functionality of the reference arena-auto-continue project.
4. Do not create duplicate systems when existing AATM functionality can be extended.
5. Preserve existing features and integrations.
6. Maintain compatibility with the existing Command Bridge.
7. Use GitHub API for branch, commit, and Pull Request detection.
8. Maintain independent session management for every employee.
9. Keep the owner approval requirement for merging.
10. Write meaningful automated tests.
11. Use separate commits for R1 through R6.
12. Push every completed implementation item.
13. Keep all work on one branch and create one Pull Request.
14. Never merge the Pull Request yourself.
Real Arena Interface Verification:

If the actual Arena interface differs from the reference implementation's heuristics, or Selector Test fails, stop and ask the owner to inspect the real interface.

Do not guess selectors or claim compatibility without verification.

The final objective is a fully integrated, dynamically scalable Arena Agent execution system inside AATM, making maximum practical use of the reference project's functionality while preserving AATM's existing architecture, security, task orchestration, GitHub review, and owner-controlled merge workflow.
