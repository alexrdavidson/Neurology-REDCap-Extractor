"""The guided app (`nmosd app`): one Streamlit page that walks a clinician through every step on this computer.

* `workspace` — the settings file and the standard folder layout under the private data folder.
* `checks`    — is this computer ready (Ollama, the model, the de-identification models, the Access reader)?
* `jobs`      — long steps run as background processes that survive closing the browser tab.
* `guide`     — the Streamlit script (the steps, plus the review screens as a second page).
"""
