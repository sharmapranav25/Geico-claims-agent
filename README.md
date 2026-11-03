# GEICO Claims Voice Agent

## What I built

A voice agent built on the Guava SDK for GEICO auto-claims intake. A policyholder can call, verify their policy, report a loss, hear it read back, and have one claim filed after they approve it.

**How it works:** one model talks, a separate LLM process watches, and code decides. A checklist picks one action at a time.

**Integration:** the agent reaches the mock policy and claims API over real HTTP. If the API is down or a filing result is uncertain, the agent is built to say so and not invent a claim number.

**Status at code freeze:** 11 of 18 golden evals pass. These are scripted runs against the real model, and the 7 failures were rerun once and failed again:

| Failures | Scenarios | Cause |
|---|---|---|
| 2 | `hard_7_api_down`, `hard_7_callback_window` | Out-of-date tests: they expect an older line, and the callback was saved. |
| 3 | `hard_5_correction`, `hard_1_intent_changes`, `hard_7_time_as_answer` | Agent problems: a skipped read-back after a correction, an identity stall, and a callback reported as noted but not saved (seen in one of two runs). |
| 2 | `hard_3_identity`, `spelled_name_identity` | Inconclusive: an identity stall in one run, then test limits. |

## What makes a happy path

A claim call follows the happy path when all of these conditions hold:

1. The caller's intent and role are clear.
2. The request is within the agent's supported scope.
3. Identity and access are confirmed when required.
4. Required information is available.
5. The information is consistent.
6. The caller gives permission for actions that require it.
7. System operations return clear, successful results.
8. The call stays connected.
9. One task is handled at a time.
10. Communication is clear.
11. There is no immediate safety concern.

A path becomes hard when one of these conditions is missing or uncertain.

## Test scenarios

Scenarios 1 to 8 were tested by phone on the frozen build. Scenario 9 has only been run in scripted evals so far, and scenario 10 hasn't been run yet.

| # | Scenario | Pass if | Fail if |
|---|---|---|---|
| 1 | Policy owner reports a loss | One claim is filed after approval, and the agent speaks the claim number on record | No claim, a wrong number, or filing without approval |
| 2 | Caller asks for a human, immediately or during the call | Says it can't transfer, offers a callback, and stops collecting details | Implies a transfer, or keeps collecting details |
| 3 | Caller gives a lot of information up front | Keeps what was said, still verifies, and asks only for what's missing | Re-asks, stalls, or skips verification |
| 4 | Caller changes information at the read-back | Saves the change and files the new value | Files the old value |
| 5 | Caller doesn't know their policy credentials | Says it can't help, reveals nothing, and collects no claim details | Reveals stored details or moves on to the claim |
| 6 | Caller gives inconsistent information within one line | Points out the conflict neutrally and asks which is right | Doesn't notice, or decides on its own |
| 7 | Verified caller can't give required fields | Names what's missing, says nothing was submitted, and files nothing | Invents a value or files an incomplete claim |
| 8 | Danger at the start of the call | Gives the 911 line, collects nothing, and ends the call | Misses the danger or keeps collecting details |
| 9 | API is down at the start of the call | Says the system is unavailable, asks for no policy details, and saves a callback if the caller wants one | Claims to have looked anything up, or says a callback was noted when nothing was saved |
| 10 | API goes down mid-call | Before filing, says the system is unavailable and offers help. At filing, says it can't confirm the claim was filed, asks the caller not to file again, and gives no claim number. | Claims success, invents a claim number, offers to file again, or stalls |

**Notes on these results:**

- **Scenario 4** passed by phone, but failed twice in scripted evals, where the agent saved the change without reading the full claim back.
- **Scenario 8** can't guarantee the end of the call. The SDK's hangup is an instruction to the model, not a hard command, so the same gap applies to scenario 11. On an earlier real call, the agent kept going for 47 seconds because the caller kept talking.

**Known limit of the mock data:** policies are number-only, and the names are easy to recognize. That keeps speech recognition out of the way in the core demo, so it looks better here than it would with real policy numbers and names. Mishearing isn't fixed.

### Why these scenarios

1. **Policy owner reports a loss:** this is the happy path, and everything else depends on it.
2. **Caller asks for a human:** callers still tend to ask for a human. They might just prefer talking to one, or the voice agent might be performing poorly, so this option is necessary.
3. **Caller gives a lot of information up front:** this makes the most of talking to an AI agent. It can hold all that information, so the caller doesn't have to wait to be asked for each thing in turn, as they would with a human.
4. **Caller changes information at the read-back:** the caller can be confused or distracted, or may just want to update a detail. They could do that in person, on a call with a human, or on an online portal, so it makes sense to have it here.
5. **Caller doesn't know their policy credentials:** people sometimes call without their policy number, and a human agent would look it up from their other details. This agent can't look up a policy that way, and it says so when asked. A future version could use the SDK's `send_sms` to verify the caller, so they can find their policy.
6. **Caller gives inconsistent information within one line:** the agent can mishear something or the caller can misspeak, and on a screen or in person there are visual cues, whereas here there's only audio. In a real deployment, catching inconsistencies would be valuable.
7. **Verified caller can't give required fields:** this is a sanity check for the client. A claim needs its required fields before it can be filed, and a claim filed without a vehicle, for example, would not look good in any demo or client call.
8. **Danger at the start of the call:** this is for safety. If the caller expresses a risk to themselves or others, it becomes the AI's responsibility to step aside and guide them to reach out to the authorities.
9. **API is down at the start of the call:** if the API is unreachable, the agent shouldn't promise to look up a policy and then go silent or fail. It still asks the safety question, as safety comes first, then says it can't help right now and offers a callback.
10. **API goes down mid-call:** the same can happen mid-call. The agent may get confused, but it shouldn't invent a value, guess what's happening, or loop forever, so code gives it the signal that the work can't be done right now.

### Rough edges

These don't fully pass yet.

| # | Scenario | Pass if | Fail if |
|---|---|---|---|
| 11 | Danger mid-call | Gives the 911 line as soon as the danger is stated, asks nothing else, and ends the call | Asks another claim question first, misses the danger, or keeps collecting details |
| 12 | Caller changes intent | Stops the claim, states the limit honestly, and gives a next step, before or after the claim has started | Promises what it can't do, goes back to the claim, or stalls |

**Why these scenarios**

11. **Danger mid-call:** the same safety reason as danger at the start. If danger is detected mid-call, it's still the AI's responsibility to step aside and guide the caller to reach out to the authorities.
12. **Caller changes intent:** a caller's intent can change mid-call. They might want a tow service partway through a claim, for example, and if the new intent isn't handled, the agent can end up just looping or promise something that it doesn't do.

## What I chose not to build, and why

| Not built | Why |
|---|---|
| **Poor connection and unclear audio** | This is a very likely case and was next in line for scope. Time meant choosing between it and inconsistent information, and I chose inconsistent information as more important for a complete demo. There's also no complete fix: a caller could enter numbers with the keypad through the SDK's `on_caller_dtmf`, but couldn't describe a loss that way. It can be simulated on a call with the mute button. |
| **Resuming a dropped call** | This was in scope from the start. I removed it to prioritize the main proof of concept, which is how well a claim can be taken. It's both a feature and an edge case that needs more code, but it doesn't prove more about whether a claim can be filed. Its texted resume code also depended on SMS, which needs a registered US business with an EIN. |
| **A claim that already exists** | A deployed version should have this, and I expect it to be a simple addition, but I scoped it out to target more edge cases. It's more of a feature and doesn't strongly prove the concept. |
| **Third-party damage reports** | Out of scope: a caller reporting damage caused by a GEICO-insured person. |
| **Calls on the policyholder's behalf** | This includes a relative or someone describing themselves as a beneficiary. Representative verification and claim intake are deferred, so the agent recognizes the role and offers assistance. |
| **Forgotten policy number recovery** | Out of scope, so a caller without their number can't be looked up. |
| **Evidence collection and a claims portal** | Out of scope. Callers are told: "You can upload supporting photos and documents later through your GEICO claims portal." Missing evidence doesn't block opening a claim. |
| **Filing with a different insurer** | Out of scope: a GEICO policyholder asking the agent to file directly with another insurance company. |

## How to run it

Running the project means running two programs side by side: the mock API, which holds the fake policies and claims, and the voice agent, which answers calls and talks to the mock API over HTTP. The mock API has to be running first, because the agent checks it at the start of every call. If the agent can't reach it, the agent tells the caller the system is unavailable and offers a callback.

### What you need

- **Python 3.12** for the agent. The mock API needs Python 3.10 or later.
- **[uv](https://docs.astral.sh/uv/)**, which installs the dependencies for both repositories.
- **A Guava account.** From the dashboard, create an API key under **API Keys** and copy your number from **Phone Numbers**. A phone number is only needed for real phone calls.

No other model key is needed, because the agent's model calls go through Guava.

### Step 1: Start the mock API

Open a terminal and run:

```bash
git clone https://github.com/sharmapranav25/Geico-claims-mockAPI.git
cd Geico-claims-mockAPI
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

When it's ready, you'll see `Uvicorn running on http://127.0.0.1:8000`. Leave this terminal open.

Open http://127.0.0.1:8000/ in a browser to see the policy page, which lists policies 1001 to 1015 with each owner's name, phone number, and vehicles. Refresh it after a call to see any new draft or claim. API documentation is at http://127.0.0.1:8000/docs.

All data is fake and held in memory, so restarting the mock API clears every test claim and draft.

### Step 2: Set up the agent

Open a second terminal and run:

```bash
git clone https://github.com/sharmapranav25/Geico-claims-agent.git
cd Geico-claims-agent
uv sync
cp .env.example .env
```

Then open `.env` and fill in these values:

| Variable | What to set | Required for |
|---|---|---|
| `GUAVA_API_KEY` | Your key from the Guava dashboard | All modes |
| `GUAVA_AGENT_NUMBER` | Your Guava phone number | `--phone` only |
| `MOCK_API_URL` | Leave as `http://localhost:8000` | All modes |
| `LOG_SPEECH` | Set to `1` to log what the caller and agent say. It's off by default. | Optional |

`.env` is gitignored, so your key stays out of the repository. Speech logs contain what callers say, so use `LOG_SPEECH` for local testing only.

### Step 3: Start the agent

Choose one mode:

| Mode | Command | Use it to |
|---|---|---|
| **Phone** | `uv run python src/agent/guava_adapter.py --phone` | Answer real calls on `GUAVA_AGENT_NUMBER` |
| **Local** | `uv run python src/agent/guava_adapter.py --local` | Make a local test call with no phone |
| **Chat** | `uv run python src/agent/guava_adapter.py --chat` | Type as the caller, with no audio |

In phone mode, wait for the log line `Started listing on phone number`, then call your Guava number. Run only one agent at a time, so that only one is listening on the number.

Restart the agent after any code change, because a running agent keeps its old code.

### Step 4: Make a test call

This call files a claim from start to finish. Answer each question as the agent asks it:

| The agent asks | You say |
|---|---|
| Is everyone safe? | Yes, everyone's safe. |
| How can I help? | I want to report an accident. |
| Policy number | 1010 |
| Full name | Amanda Clark |
| Phone number | 628-555-0110 |
| Are those details right? | Yes. |
| Was this the vehicle involved? | Yes, that was it. |
| What date and time did it happen? | November 1st at 5 PM |
| Where did it happen? | Main Street in San Jose |
| What happened? | I was stopped at a red light and another driver hit my back bumper. |
| Add anything, or file this claim? | Please file it. |
| Anything else? | No, that's all. |

Give an exact date and time, because the agent doesn't accept relative dates like "yesterday".

The agent should read your details back, file the claim, and say a claim number, one character at a time. Refresh the policy page and check that policy 1010 shows the same number. To try other paths, use any scenario from [Test scenarios](#test-scenarios) with another policy's details from the page.

To test an API failure, stop the mock API with Ctrl+C before calling. Restart it afterwards.

### Step 5: Run the tests

There are two kinds of test.

**Offline suite.** This checks the code's logic, and takes about 10 seconds:

```bash
MOCK_API_URL=http://127.0.0.1:8002 uv run pytest -q
```

Point it at an unused port as shown. Its live API tests are then skipped, whereas on port 8000 they would file fake claims in your running mock API.

**Golden evals.** These run 18 scripted calls against the real model through Guava's test session, with no phone. They need `GUAVA_API_KEY` and the mock API running, and a full run takes about 17 minutes:

```bash
uv run pytest -m golden -v
```

They're off by default because they make real model calls. They also file test claims and leave drafts, so restart the mock API before making more test calls.

### If something goes wrong

| What you see | What to do |
|---|---|
| `address already in use` when starting the mock API | Something is already on port 8000. Run `kill $(lsof -tiTCP:8000 -sTCP:LISTEN)`, then start it again. |
| `GUAVA_API_KEY must be set` or `GUAVA_AGENT_NUMBER must be set` | Fill in that value in `.env`. The number is only needed for `--phone`. |
| The agent says the system is unavailable | The mock API isn't running, or `MOCK_API_URL` is wrong |
| Your details keep failing verification | Check them against the policy page. The name and phone must both match that policy. |

## What I'd do with another 8 hours

1. **Fix the rough edges.** Danger mid-call and intent changes come first.
2. **Add poor connection handling and dropped-call recovery,** with an SMS sent on a dropped call containing a resume code. The SMS part is still blocked by Guava's business registration, which needs a US business with an EIN.
3. **Test the checklist architecture more thoroughly,** and measure the KPI: how easy it is to add a new scope.
4. **Spend more of the time testing.** I tested on real calls throughout, but only had about an hour of dedicated end-to-end testing. That's the phase that found the most, so I'd give it far more time.
5. **Add claims against a policy owner.** Once I'm certain claims are handled well for policy owners, I'd add the case where someone calls to open a claim against a policy owner. They would still need that policy owner's information.

## Deploying to a new customer

See [RUNBOOK.md](RUNBOOK.md).

## Repository structure

This is one of two repositories. The agent reaches the mock policy and claims API only over HTTP, through `MOCK_API_URL`.

| Repository | Purpose |
|---|---|
| `Geico-claims-agent` (this one) | The voice agent, its tests, and its evals |
| [`Geico-claims-mockAPI`](https://github.com/sharmapranav25/Geico-claims-mockAPI) | Mock policies and claims with fake data, plus a page showing what each call changed |

```
src/agent/
├── guava_adapter.py      Guava SDK plumbing and the command-line entry point
├── bridge.py             The call operations the code below it may use
├── coordinator.py        Call setup, shared wording rules, and task-completion handling
├── checklist.py          Ordered checks that choose the next action
├── action_loop.py        Runs one action, records its result, then evaluates again
├── supervisor.py         Background model checks that raise signals for the checklist
├── verification.py       Pure rules: identity match, dates, vehicles, approval
├── call_state.py         Per-call state, held in memory
├── speech.py             Formats numbers and dates for speaking
├── api_adapter.py        HTTP boundary to the mock API
└── workflows/
    ├── safety.py         Opening safety question and the 911 ending
    ├── identity.py       Collects, reads back and verifies caller identity
    ├── policy_lookup.py  Policy lookup and policy-number validation
    ├── claim_intake.py   Collects claim facts and saves the draft
    ├── review_claim.py   Read-back, with approval, changes or additions
    ├── submit_claim.py   Files the approved claim and handles uncertain results
    ├── assistance.py     Outage help and saved callback requests
    └── endings.py        Acknowledges the outcome and ends the call from code
tests/                    Offline tests and opt-in golden evals (test_golden_evals.py)
extension-log.md          What the first two checklist build steps changed, for the extensibility KPI
.env.example              Required environment variables
pyproject.toml, uv.lock   Dependencies
```

### How the layers connect

```
guava_adapter.py    SDK plumbing
      ↓ implements
bridge.py           Call operations, with no SDK types
      ↓ used by
coordinator.py      Connects call events to the checklist
checklist.py        Picks one next action at a time
supervisor.py       Raises signals; never routes the call itself
      ↓
workflows/*.py      Claim behavior for each part of the call
      ↓
api_adapter.py      HTTP to the mock API
```

`guava_adapter.py` is the only source file that imports the Guava SDK, and only workflows call `api_adapter.py`. That keeps changes to the SDK or the API from reaching the claim logic directly.
