# Rules changelog

Every rule pack change is a release with its own PR, second reviewer (CODEOWNERS) and entry here.

## 2026.10.0 (19 Oct 2026)

- Initial packs: `rbi.sma`, `rbi.irac`, `rbi.crilc`, `rbi.cersai`, `statute.companies_act_chg1`,
  `rbi.document_release`, `rbi.rfa`, `rbi.wilful_defaulter`, `rbi.prudential_framework`,
  `rbi.penal_charges`, `rbi.large_exposures`, `rbi.ecl`, `policy.credit_policy`,
  `policy.approval_matrix`, `policy.covenant_consequences`, `policy.monitoring`, `ews.indicators`.
- **All packs are `status: unverified`.** Parameters were taken from the build guide, which cites
  them from memory. UAT and production refuse to load them until a named person checks each
  source on rbi.org.in (or the statute), fills `source.url`, `verified_by`, `verified_on`, and
  flips `status: verified`.
- Policy packs (`policy.*`) carry illustrative parameters and must be replaced with the bank's
  board-approved credit policy and delegation of financial powers.
