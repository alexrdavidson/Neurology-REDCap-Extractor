# Using a frontier cloud model (e.g. Claude) for extraction: feasibility notes

Researched 18.11.26 from public sources only. This is background for a conversation with MGB, not legal or
compliance advice, and it changes nothing in the project rules: runtime network access is still Ollama at
127.0.0.1 only (BUILD_PLAN §0).

## Question

Could we de-identify the notes and send them to Claude (e.g. Opus) to fill in the codebook, because the local
model handles simple fields well but struggles with complex ones?

## Short answer

- **HIPAA** allows it only if the data are truly de-identified, but **MGB policy, the IRB protocol and the RPDR
  terms** can still forbid it. Only MGB can approve it.
- **Sending de-identified notes to an outside service is unlikely to be approved**, and we found no MGB example
  of anyone doing it.
- **What MGB researchers actually do** is run a frontier model on identifiable notes *inside* MGB's agreements:
  Azure OpenAI under MGB's BAA with Microsoft, on a private network endpoint. That route is realistic for us,
  and it keeps real dates. The model on offer there has been GPT-class; whether Claude is available inside MGB
  is unconfirmed.

## HIPAA background

- De-identified data are not PHI, so HIPAA no longer applies to them. There are two methods:
  - **Safe Harbor**: remove the 18 identifiers, including every date element finer than the year, and have no
    actual knowledge that the rest could identify someone.
  - **Expert Determination**: a qualified expert certifies, in writing, that re-identification risk is very
    small.
- Data that are not de-identified are PHI. Sending them to a vendor needs a Business Associate Agreement (BAA).
  Anthropic signs BAAs only for business setups: the API with its HIPAA configuration, or HIPAA-ready Enterprise
  plans. Claude Code has extra conditions on top of that. Personal Free, Pro and Max accounts have no BAA.

## Why our current de-identified notes would not qualify as they are

1. **Shifted dates are not Safe Harbor.** Our pipeline shifts each patient's dates by a fixed offset
   ([date_shifting.md](date_shifting.md)). HHS guidance allows nothing finer than the year under Safe Harbor,
   so shifted notes would need an Expert Determination. Cutting dates to the year would break the codebook's
   date fields (onset, relapses, treatment starts).
2. **Free-text scrubbing is imperfect.** Philter catches about 99.5% of identifiers on benchmark corpora. A 2024
   study still re-identified about 9% of notes after the best redaction tool.
3. **NMOSD is rare.** A detailed disease course can identify a patient on its own (the "actual knowledge"
   clause).
4. **Institutional rules sit on top of HIPAA.** The data came from RPDR under an IRB protocol. Either may
   restrict where even de-identified data go. MGB's research generative-AI guidelines reportedly forbid PHI in
   any public AI platform. The document is behind the MGB portal login and has not been read.
5. **Outside precedent is strict too.** PhysioNet, which hosts the de-identified MIMIC notes, bars sending them
   to third-party AI services unless there is zero retention, no training use and no human review, and it
   recommends local models.

## Similar work at MGB

| Study | Task | Model and route | De-identified first? |
|---|---|---|---|
| RECTIFIER, BWH + MGB research computing (preprint 2024; NEJM AI) | Clinical-trial screening from notes, 382 patients | GPT-4 through MGB's private Azure OpenAI instance | No: identifiable notes, "minimum necessary" PHI |
| Du, Zhou et al., BWH (eBioMedicine 2024) | Detecting cognitive decline in notes | GPT-4 through Azure OpenAI; Llama 2 on an MGB-controlled cloud server | Not stated; "HIPAA-compliant" platforms |
| MGB + Dana-Farber (Nature Cancer 2026) | Immune-related adverse events | GPT-4.1 through an "institutional HIPAA-compliant API" | Not stated |
| Guevara, Bitterman et al., MGB/Dana-Farber (npj Digit Med 2024) | Social determinants of health in notes | Mainly locally fine-tuned open models (Flan-T5) | Not applicable for the local models |

The RECTIFIER safeguards, as written in its methods:
- a BAA between MGB and Microsoft;
- a private endpoint restricted to MGB's virtual network;
- APIs that did not persist prompts or responses;
- MGB IRB approval.

Other public signals:
- MGB has an internal multi-model platform for staff, the "AI Zone".
- MGB has a "landing zone" for testing AI products.
- MGB has an Azure Enclave with an OpenAI workspace for researchers.
- MGB rolls out Microsoft Copilot across the organization.

None of these sources names Anthropic or Claude.

## Routes and likelihood

| Route | Likelihood | Notes |
|---|---|---|
| This session, or any personal Claude account | Very unlikely | No BAA; outside MGB's agreements |
| De-identified notes to the Anthropic API under an MGB contract | Uncertain | Would need an MGB–Anthropic agreement, Expert Determination for shifted dates, and IRB/privacy sign-off |
| GPT-class model via MGB's secure Azure setup or AI Zone | Likely | Clear precedent; no de-identification needed, so real dates are kept |
| Claude inside MGB (Snowflake Cortex, AWS Bedrock, MGB–Anthropic contract) | Unknown | Snowflake Cortex hosts Claude models; it depends on what MGB has enabled |
| Harvard Medical School's Claude licence | Unlikely to apply | HMS approves Claude up to its "Level 3" data. MGB patient data are governed by MGB and RPDR terms |

## Realistic expectations

- Frontier models score roughly 95–100% on well-defined questions in the MGB studies. That is not a guarantee
  for NMOSD's hard fields: relapse counts and dates, treatment start/stop, new MRI lesions.
- Many errors on those fields come from missing or contradictory notes and from codebook ambiguity, which a
  stronger model does not fix. Clinician review of every value stays.
- The expected gain is fewer corrections at review, not a hands-off tool.
- A hybrid is reasonable: keep the local model for simple fields and send only hard fields to an approved
  frontier model. That also shrinks the volume going to the approved service.

## Who to contact

Start with the PI, who holds the protocol and would file any amendment. Approvals usually attach to named
study staff, so also confirm that a placement appointment covers this.

| Who | Ask | Contact (from public documents; verify on the intranet) |
|---|---|---|
| IRB (Human Research Office) | Does the protocol allow an outside AI service, with de-identified or identified data? Is an amendment needed? | partnersirb@partners.org / IRB@partners.org |
| Privacy Office | Do date-shifted notes need an Expert Determination, and can one be arranged? Can they route the question to a research privacy contact? | PrivacyOffice@mgb.org, 855-726-1300 |
| ERIS Research Computing | Can we get research access to the secure LLM service used in the studies above? Is Claude available anywhere inside MGB? | rc.partners.org (now redirects to the MGB research portal) |
| RPDR | Do the data-use terms allow processing extracts outside MGB systems? | MGBAnalyticsEnclaveSupport@mgb.org |

Some of these addresses use the older partners.org domain.

## Next steps

1. Run the evaluation on the pilot patients and record accuracy **per field**. This gives concrete evidence
   for the request, e.g. "95% on demographics, 60% on relapse dates".
2. Talk to the PI, then email the IRB and Privacy Office together, and ERIS separately about tools.
3. If access is approved: add an MGB endpoint behind the existing LLM client interface. This needs a
   BUILD_PLAN / CLAUDE.md rule change to allow that host. Do not make that change before written approval.
4. Ask the same IRB/Privacy question about the quality packets (BUILD_PLAN §0 rule 8). They rest on the same
   unconfirmed approval.

## Sources

- [HHS: Methods for de-identification of PHI](https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html)
- [Hunton summary of the HHS de-identification guidance](https://www.hunton.com/privacy-and-cybersecurity-law-blog/hhs-publishes-guidance-on-how-to-de-identify-protected-health-information)
- [Anthropic: BAAs for commercial customers](https://privacy.claude.com/en/articles/8114513-business-associate-agreements-baa-for-commercial-customers%20%20)
- [Anthropic: HIPAA and Claude](https://privacy.claude.com/en/articles/8956058-i-need-to-be-hipaa-compliant-can-i-use-claude)
- [Anthropic: Covered models under the BAA](https://support.claude.com/en/articles/15455031)
- [RECTIFIER preprint (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC10871450/)
- [Du et al., cognitive decline study (PubMed)](https://pubmed.ncbi.nlm.nih.gov/39396423/)
- [Immune-related adverse events study, Nature Cancer](https://www.nature.com/articles/s43018-026-01264-6)
- [Guevara et al., social determinants study (arXiv)](https://arxiv.org/abs/2308.06354v2)
- [MGB Generative AI Guidelines (login required; not read)](https://researchhub.mgb.org/wp-content/uploads/2025/02/Generative-AI-Guidelines.pdf)
- [Healthcare IT News on MGB's AI Zone (could not be fetched)](https://www.healthcareitnews.com/news/ai-embedded-across-enterprise-valuable-perspective-mass-general-brigham)
- [VentureBeat on MGB's AI governance](https://venturebeat.com/orchestration/how-massmutual-and-mass-general-brigham-turned-ai-pilot-sprawl-into)
- [TechTarget on MGB's secure ChatGPT access](https://www.techtarget.com/searchnetworking/feature/Mass-General-Brigham-tackles-network-upgrade-AI-pilots)
- [MedCity News on MGB's AI work](https://medcitynews.com/2026/03/mass-general-brigham-ai)
- [Philter de-identification paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC7156708)
- [Re-identification risk study (arXiv)](https://arxiv.org/pdf/2407.16166)
- [PhysioNet: MIMIC data with LLMs and online services](https://physionet.org/news/post/llm-responsible-use/)
- [HMS generative AI guidance](https://it.hms.harvard.edu/about/policies-and-guidelines/generative-ai)
- [Snowflake and Anthropic announcement](https://www.snowflake.com/en/news/press-releases/snowflake-and-anthropic-accelerate-enterprise-ai-adoption-driven-by-rising-demand-for-governed-ai/)
- [MGB IRB Guidance (IRB contact)](https://www.massgeneralbrigham.org/content/dam/mgb-global/en/research-and-innovation/for-researchers-and-collaborators/documents/Mass%20General%20Brigham%20IRB%20Guidance.pdf)
- [MGB Privacy Office letter (contact details)](https://www.mass.gov/doc/2026-1572-mass-general-brigham/download)
- [PDSR / RPDR data set contact](https://rc.partners.org/node/3986)
