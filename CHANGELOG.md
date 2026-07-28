# Changelog

## [1.23.0](https://github.com/future-agi/future-agi/compare/v1.22.76...v1.23.0) (2026-07-28)


### Features

* **model-hub:** add claude 5 and gemini 3.x catalog entries ([306c52e](https://github.com/future-agi/future-agi/commit/306c52efebe15267248331816c0bf01090c6bb4e))
* **model-hub:** add claude 5 and gemini 3.x catalog entries ([9b92a96](https://github.com/future-agi/future-agi/commit/9b92a96259bfde159e3360c18e4eaf103224412f))
* **model-hub:** add gemini 3 pro/flash base and image-gen entries ([e439da4](https://github.com/future-agi/future-agi/commit/e439da44f8667054bd215a6c4e7d732b90136eda))
* **models:** register Gemini 3.6 Flash + add pricing for the new models [TH-7193/TH-7195] ([#1818](https://github.com/future-agi/future-agi/issues/1818)) ([be7cc72](https://github.com/future-agi/future-agi/commit/be7cc72a947baacca2dcae9347dbb9fda9ba9ad7))
* **simulate:** add Bland.ai as an inbound voice provider ([08b40f3](https://github.com/future-agi/future-agi/commit/08b40f32e84743cd122066f7236d074561aa5b0c))
* **simulate:** support Bland as an outbound customer provider ([dfa9c72](https://github.com/future-agi/future-agi/commit/dfa9c720c89c868e3e079ce78a612cfd3ee5cd1e))


### Bug Fixes

* **derived-variables:** gate tolerant JSON parsers on structural chars (TH-6975) ([91c8caf](https://github.com/future-agi/future-agi/commit/91c8cafc1dc3dbe2f0e8e8ae7bf87bd7d2417c3d))
* **evals:** derive provider from model in CustomPromptEvaluator, guard call_llm on None provider ([000120f](https://github.com/future-agi/future-agi/commit/000120f792852293631752f217c09555d7deec38))
* **evals:** derive provider from model in CustomPromptEvaluator; guard call_llm on None provider ([ed8cbf6](https://github.com/future-agi/future-agi/commit/ed8cbf6684cea98cdbe9163699a389f219c385ce))
* **evals:** preserve typed/pasted JSON in eval Test Data editor ([#1727](https://github.com/future-agi/future-agi/issues/1727)) ([49c9457](https://github.com/future-agi/future-agi/commit/49c94575bd87a3638623056bb535762d4d1a2821))
* **merge-guard:** push tip via github.event.after (twin of [#1803](https://github.com/future-agi/future-agi/issues/1803)) ([a99b3f3](https://github.com/future-agi/future-agi/commit/a99b3f3edbf2bfb130222160b619ee758810d1be))
* **merge-guard:** read push tip via github.event.after, not .commits[].sha ([b2dae1b](https://github.com/future-agi/future-agi/commit/b2dae1b901c76070a2b2523aa5b1adef6c06700b))
* **merge-guard:** read push tip via github.event.after, not .commits[].sha ([f326c9f](https://github.com/future-agi/future-agi/commit/f326c9f6d1bf7bf1ef32613068f0dc3a4afe7ed6))
* **merge-guard:** read push tip via github.event.after, not .commits[].sha (twin of [#1803](https://github.com/future-agi/future-agi/issues/1803)) ([d345fd3](https://github.com/future-agi/future-agi/commit/d345fd3b39aad4b3be59b92450ca7ee04b414fd2))
* **observe:** reduce list page_size to 25 and trim load-time over-fetch (TH-7155) ([#1747](https://github.com/future-agi/future-agi/issues/1747)) ([4106d33](https://github.com/future-agi/future-agi/commit/4106d33f79f867eb238f1c0dd2aa8e28275a8bea))
* **simulate:** play combined-only voice recordings instead of spinning forever ([4ce647f](https://github.com/future-agi/future-agi/commit/4ce647f7eeb7520ce8e1787839fb30feeddc41f3))
* **test:** drop eslint-disable for a rule this config does not define ([577e07d](https://github.com/future-agi/future-agi/commit/577e07d051b9df0c6ce6696263aba54c8a6feacb))
* **TH-7128:** dataset test suite cleanup — 4 code fixes, 17 failures resolved, 143→64 test consolidation, 10 renames ([8b2271a](https://github.com/future-agi/future-agi/commit/8b2271a81a38caa07104d666bed066a7d785185f))
* **tracer:** scope voice call detail to the request org, prefer rehosted Bland recording ([e52081f](https://github.com/future-agi/future-agi/commit/e52081f4d4a23cd2a6b4a82c7d5e4c6a79413b79))
* **voice:** scope call detail to request org, play combined-only recordings, prefer rehosted Bland URL ([8714d05](https://github.com/future-agi/future-agi/commit/8714d05d353a39ca1c40d5d38869c57c2ac65eea))


### Performance Improvements

* **tracer:** add mapValues bloom indexes for span-attribute filters ([1aa78e5](https://github.com/future-agi/future-agi/commit/1aa78e5ef141d6814119fb74ea069fc53331532e))
* **tracer:** bound attr-filter membership subqueries to project + window ([4f75d61](https://github.com/future-agi/future-agi/commit/4f75d61103993508d5244dae63e3dedcb1151c36))
* **tracer:** scope attr-filter subqueries + mapValues bloom indexes ([a1f4c44](https://github.com/future-agi/future-agi/commit/a1f4c4495711039dd2268c2bc6274aa3a0b41de4))
* **tracer:** serve case-insensitive text filters from a lowered value bloom ([90d5b1a](https://github.com/future-agi/future-agi/commit/90d5b1a0fa2f5719023165547688e17a23b1c265))
