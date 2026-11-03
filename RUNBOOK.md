# Deploying to a new customer

This is the runbook I'd follow. I haven't done it for a second customer, so the amount of change is an estimate.

**0. Ask the customer first**

Before any code changes, find out:

- Their claims API endpoints, authentication method, and required fields.
- Whether they can do a live transfer. This build only offers callbacks because there's no transfer to connect to.
- Their verification rules, and any compliance or recording requirements.
- Their business hours for callbacks.
- Their expected call volume.

**1. Connect the Guava account**

- Set the customer's `GUAVA_API_KEY` and `GUAVA_AGENT_NUMBER` in `.env`.
- If the agent will send SMS, complete Guava's business registration and outbound approval first.

**2. Connect the customer's systems**

- Point `MOCK_API_URL` at their API, and update `api_adapter.py` to match its endpoints and response models. The workflows only call this file, so most API changes stay there. The variable name is a known leftover from the demo, and I'd rename it `CLAIMS_API_URL` for production.
- Add authentication, since the current adapter sends none.
- Replace the local callback file (`CALLBACK_FILE`, `data/callbacks.jsonl` by default) with the customer's scheduling system.
- Replace the demo identity check in `verification.py`. It only matches the stated details against one record, and doesn't prove who the caller is.

**3. Change what's specific to the customer**

- **Name and pronunciations:** these are set where the SDK agent is built, in `build_agent` in `guava_adapter.py`. I'd move them to config next, so a new customer doesn't need to touch the SDK file.
- **Purpose and wording:** `coordinator.py` and the lines in `workflows/`, including `submit_claim.py`, `assistance.py`, and `endings.py`.
- **Required claim fields:** `REQUIRED_FACTS` and the fields in `workflows/claim_intake.py`.
- **Callback hours:** the rules in `verification.py` and the wording in `workflows/assistance.py`.

**4. Prepare for real callers**

- Keep `LOG_SPEECH` off, or mask caller details first, because logs can contain what callers say.
- Call state is held in memory for one process, so move it to a shared store before running more than one instance.

**5. Test before going live**

1. Run the offline suite.
2. Update the golden evals with the customer's data and wording, then run them.
3. Call the agent by phone for every scenario in [Test scenarios](README.md#test-scenarios), and check each result in the customer's system.
4. Test with real policy numbers and names, since the mock data makes speech recognition look better than it is.

Go live only when every scenario passes by phone and the golden evals show no agent failures.

**6. Go live and monitor**

- Start with a small share of calls.
- Review call logs for false statements, and track these numbers:
  - Claims filed per call.
  - Escalation and callback rate.
  - Calls that stalled or ended without an outcome.
  - Filed claims that differ from the read-back.
- To roll back, stop the agent and route calls back to the existing line.
