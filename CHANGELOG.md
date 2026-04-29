# Changelog

## [0.7.0](https://github.com/ax-platform/ax-gateway/compare/v0.6.0...v0.7.0) (2026-04-29)


### Features

* **agents:** ax agents check &lt;name&gt; — AVAIL-CONTRACT v4 forward-compat consumer ([#101](https://github.com/ax-platform/ax-gateway/issues/101)) ([d5988e1](https://github.com/ax-platform/ax-gateway/commit/d5988e15ad6ee8b8e3ad7b887d066cc4ee8e8be9))
* **agents:** ax agents list --availability — bulk AVAIL-CONTRACT v4 consumer ([#103](https://github.com/ax-platform/ax-gateway/issues/103)) ([486e07d](https://github.com/ax-platform/ax-gateway/commit/486e07d7f642820c1d6b810a7514e385d32f153d))
* **agents:** ax agents placement get/set — GATEWAY-PLACEMENT-POLICY-001 CLI consumer ([#105](https://github.com/ax-platform/ax-gateway/issues/105)) ([32f0aa3](https://github.com/ax-platform/ax-gateway/commit/32f0aa31fe3c252e204e65a7d1f62027ede1556f))
* **context:** ax context promote &lt;key&gt; — closes the late-promotion vault gap ([#106](https://github.com/ax-platform/ax-gateway/issues/106)) ([2946497](https://github.com/ax-platform/ax-gateway/commit/2946497dcd5348bd88733788e215d2d6cec12720))
* **gateway:** listener for agent.placement.changed SSE event (8467ec87) ([#110](https://github.com/ax-platform/ax-gateway/issues/110)) ([015eac3](https://github.com/ax-platform/ax-gateway/commit/015eac3e83fec43b60f7dab19072b6944c127030))
* **gateway:** runtime install endpoint + CLI — closes AUTOSETUP-001 demo blocker ([#107](https://github.com/ax-platform/ax-gateway/issues/107)) ([e856679](https://github.com/ax-platform/ax-gateway/commit/e856679ebba9041170ea0469b5e031efcbb61b3c))
* **gateway:** time out stalled runtime messages ([a063da9](https://github.com/ax-platform/ax-gateway/commit/a063da9b9897684166635c7eee944d5561a32b4f))
* **heartbeat:** HEARTBEAT-001 — local-first agent heartbeat CLI primitive ([#100](https://github.com/ax-platform/ax-gateway/issues/100)) ([181be6c](https://github.com/ax-platform/ax-gateway/commit/181be6cc6bb531612c76c7d109eb34df56579673))
* **messages:** ax send post-send delivery_context chip — closes AVAIL-CONTRACT v4 send-time UX ([#104](https://github.com/ax-platform/ax-gateway/issues/104)) ([2aa89a7](https://github.com/ax-platform/ax-gateway/commit/2aa89a7c383613dacc33e4e88b00f9dc375740a2))
* **reminders:** TASK-LOOP-001 — priority queue + HITL draft mode ([#98](https://github.com/ax-platform/ax-gateway/issues/98)) ([3284275](https://github.com/ax-platform/ax-gateway/commit/328427535675b04641ae8453a39221d6efb1a55a))
* **reminders:** TASK-LOOP-001 v1.1 — offline-first follow-up ([#99](https://github.com/ax-platform/ax-gateway/issues/99)) ([815c2cc](https://github.com/ax-platform/ax-gateway/commit/815c2ccbbb670b1346c452a04041b15d19b039cb))
* **specs:** pre-staged smoke for placement contract — task 2598129a ([#111](https://github.com/ax-platform/ax-gateway/issues/111)) ([504c11a](https://github.com/ax-platform/ax-gateway/commit/504c11ab91df3aa71b9b3d8f3f42c4a1ace5d13b))


### Bug Fixes

* **bootstrap:** ensure-agent semantics — 409 falls back to GET-by-name ([#109](https://github.com/ax-platform/ax-gateway/issues/109)) ([ecc8776](https://github.com/ax-platform/ax-gateway/commit/ecc87762e540f7b98856499531589af47adc0410))
* **gateway:** surface actionable error when python3-venv is missing ([#108](https://github.com/ax-platform/ax-gateway/issues/108)) ([cfda451](https://github.com/ax-platform/ax-gateway/commit/cfda4513ab941fe795616b10c7f89195391dad6e))
* **tasks:** resolve target spaces by slug ([#120](https://github.com/ax-platform/ax-gateway/issues/120)) ([aa9239b](https://github.com/ax-platform/ax-gateway/commit/aa9239b9028735f785d268be5cfdc58817e88f01))


### Documentation

* **inventory:** CLI-SURFACE-INVENTORY-001 v1 — closes 653cae21 ([#102](https://github.com/ax-platform/ax-gateway/issues/102)) ([22ec3e2](https://github.com/ax-platform/ax-gateway/commit/22ec3e28b88e640ecc77127c0e8344e5bb181e0c))
* **rfc:** GATEWAY-CONNECTION-MODEL-001 — phased connection model RFC stub ([#94](https://github.com/ax-platform/ax-gateway/issues/94)) ([c13de3f](https://github.com/ax-platform/ax-gateway/commit/c13de3f88e7fbd3608cffb04f583e46ffc1fda60))
* **spec:** AGENT-AVAILABILITY-CONTRACT-001 — cross-surface presence contract ([#96](https://github.com/ax-platform/ax-gateway/issues/96)) ([3f0d822](https://github.com/ax-platform/ax-gateway/commit/3f0d82269d1f6b0550ee5642dff9a62af64575c1))
* **spec:** AVAIL-CONTRACT v4 — pair with backend GATEWAY-PRESENCE-DATA-MODEL-001 ([#97](https://github.com/ax-platform/ax-gateway/issues/97)) ([9ebebee](https://github.com/ax-platform/ax-gateway/commit/9ebebee7dffbec50a42f5d235a31fe34263f881f))
* **spec:** GATEWAY-PLACEMENT-POLICY-001 — outline + bidirectional sync model ([#95](https://github.com/ax-platform/ax-gateway/issues/95)) ([b155ea1](https://github.com/ax-platform/ax-gateway/commit/b155ea16a4875af7bfdf0d6b3249153c7590503d))

## [0.6.0](https://github.com/ax-platform/ax-cli/compare/v0.5.0...v0.6.0) (2026-04-23)


### Features

* **cli:** bootstrap agents with scoped setup flow ([#79](https://github.com/ax-platform/ax-cli/issues/79)) ([7f83ec6](https://github.com/ax-platform/ax-cli/commit/7f83ec6bcf962192da870bd10b840595b7d0b684))
* **examples/hermes:** emit AX_GATEWAY_EVENT phase events for tool calls ([5eacade](https://github.com/ax-platform/ax-cli/commit/5eacade98e6d78bf295ed4236b8a03fdf5708d54))
* **examples/hermes:** extract salient tool args into activity text ([32a90fb](https://github.com/ax-platform/ax-cli/commit/32a90fb8eaab39e1e831bd62404f94d6fed51c01))
* **gateway:** add local control plane mvp ([3223e05](https://github.com/ax-platform/ax-cli/commit/3223e050a1123e2362c0c570667cb7715c4fd092))
* **gateway:** preserve hermes sentinel runtime ([db088f3](https://github.com/ax-platform/ax-cli/commit/db088f3c68f915b2fac38e12762ff94a676cac68))


### Bug Fixes

* **channel:** deliver hermes streamed final replies, drop progress chunks ([542c040](https://github.com/ax-platform/ax-cli/commit/542c040ab3ddc65f17a66d50182eccb2c255dda7))
* **channel:** filter hermes progress messages, deliver streamed final updates ([1cf5a44](https://github.com/ax-platform/ax-cli/commit/1cf5a44d2f8bef11e30f4982fce55018009c670b))
* **channel:** materialize shared objects for agents ([b9c133c](https://github.com/ax-platform/ax-cli/commit/b9c133cea5fb6a71db16d3899b9991520c42a1bd))
* **context:** reject app shells for binary downloads ([783a705](https://github.com/ax-platform/ax-cli/commit/783a70588a631ba8e9246136eccac4276b17ded4))
* **context:** reject app shells for binary downloads ([5fb1850](https://github.com/ax-platform/ax-cli/commit/5fb18504c1db76873f5980311ff3f39a526c0b5e))
* **examples/hermes:** redirect hermes stdout/stderr during run so terminal chatter doesn't leak into reply ([a7609f1](https://github.com/ax-platform/ax-cli/commit/a7609f10d0551577af1aeb840d47eefba74eb2e6))
* **gateway:** enforce agent-bound runtime tokens ([a5bf1e2](https://github.com/ax-platform/ax-cli/commit/a5bf1e2fb5d6855a4e683e634d7fd960a1284ad3))


### Documentation

* **channel:** clarify runtime compatibility ([#62](https://github.com/ax-platform/ax-cli/issues/62)) ([b2e5388](https://github.com/ax-platform/ax-cli/commit/b2e53883b9eca35ade88b25acf969f53faa64269))
* **claude-md:** add current-state header, align on main/paxai.app ([5d7d2be](https://github.com/ax-platform/ax-cli/commit/5d7d2bea07babf31aadc2ff33439e00a83d4ed84))
* **claude-md:** add current-state header, align on main/paxai.app ([6da78d0](https://github.com/ax-platform/ax-cli/commit/6da78d0a2bd8106a2ea2be24998c11b2eba3da5c))
* make ax-cli skill shareable ([#78](https://github.com/ax-platform/ax-cli/issues/78)) ([f2de1cb](https://github.com/ax-platform/ax-cli/commit/f2de1cbf8d4bed7996f140d4cc60b7c20897095c))
* point user guides at paxai.app ([#82](https://github.com/ax-platform/ax-cli/issues/82)) ([365a433](https://github.com/ax-platform/ax-cli/commit/365a433844e1998f6dac20d319cde99da9d06875))

## [0.5.0](https://github.com/ax-platform/ax-cli/compare/v0.4.0...v0.5.0) (2026-04-17)


### Features

* **alerts:** ax alerts CLI — Activity Stream alerts + reminders MVP ([#53](https://github.com/ax-platform/ax-cli/issues/53)) ([fb0a9e5](https://github.com/ax-platform/ax-cli/commit/fb0a9e5e6d7fda57f0861937f58cc23a620e01f5))
* **alerts:** embed task snapshot in reminder alert metadata ([#54](https://github.com/ax-platform/ax-cli/issues/54)) ([#57](https://github.com/ax-platform/ax-cli/issues/57)) ([834fc4f](https://github.com/ax-platform/ax-cli/commit/834fc4fef5ed6d6ae4093a918375fe2dc667f498))
* **apps:** signal MCP app panels from CLI ([20be8ba](https://github.com/ax-platform/ax-cli/commit/20be8ba347b9e5fa08c0956f49e6d4c0d21d50f6))
* **cli:** add agent contact ping ([30218c2](https://github.com/ax-platform/ax-cli/commit/30218c26e5dfe3dfa2225629ca32122f4cc8a1c7))
* **cli:** add API contract smoke harness ([0d15bb4](https://github.com/ax-platform/ax-cli/commit/0d15bb44ae6989c0b3c2972baf5350b7994eb409))
* **cli:** add auth config doctor ([7ce6fb2](https://github.com/ax-platform/ax-cli/commit/7ce6fb220121b9ece52ce74788f3d15b0abf3372))
* **cli:** add bounded handoff loop ([70548da](https://github.com/ax-platform/ax-cli/commit/70548da085bf9d4f514ad302c5309bcdcfe332b8))
* **cli:** add environment QA matrix ([bf114cf](https://github.com/ax-platform/ax-cli/commit/bf114cf16c701444db27fab910aabd01dcead2cc))
* **cli:** add mention signals to primary actions ([0f77490](https://github.com/ax-platform/ax-cli/commit/0f77490ac65a9e65f06049feb93099748073e846))
* **cli:** add mesh discovery and adaptive handoff ([3a4c779](https://github.com/ax-platform/ax-cli/commit/3a4c77962ad757fa3a1b487cb8eff3ab09f92911))
* **cli:** add QA preflight gate ([45e1db7](https://github.com/ax-platform/ax-cli/commit/45e1db73cb3c14d81005b8a7daae29b80c66c93c))
* **cli:** make adaptive handoff the default ([247df03](https://github.com/ax-platform/ax-cli/commit/247df031f08e4d950a12a5f09b4f694317aeecbe))
* **cli:** resolve task assignment handles ([77b2e28](https://github.com/ax-platform/ax-cli/commit/77b2e28c5cb9f0380f539f03030c224dc94bb9d2))
* **cli:** select QA user login environment ([44c2d5e](https://github.com/ax-platform/ax-cli/commit/44c2d5efa2e731c7372ef75bf458c68ae4d92d2b))
* **cli:** standardize operator QA envelopes ([6236d8f](https://github.com/ax-platform/ax-cli/commit/6236d8f76a826bc102226c4e7caec0f1e223e1e5))
* harden agent team token setup ([254998c](https://github.com/ax-platform/ax-cli/commit/254998cc3c4e504b9663e1e4eb94402d941ce77d))
* make axctl login interactive ([46237a0](https://github.com/ax-platform/ax-cli/commit/46237a08d0f02e3abe3aaa6ca1d1e58ea99e77d7))
* **messages:** add ask-ax send flag ([b0a5ef7](https://github.com/ax-platform/ax-cli/commit/b0a5ef7b3a4a34ae15da4ad313ce59edf0f0e010))
* **messages:** expose unread inbox controls ([61ca0bf](https://github.com/ax-platform/ax-cli/commit/61ca0bf0c652198a48b232829a07fc4daa81ce64))
* **reminders:** lifecycle-aware source task check ([#58](https://github.com/ax-platform/ax-cli/issues/58)) ([3dcc07a](https://github.com/ax-platform/ax-cli/commit/3dcc07a5b4238f3b0d0cef5b90ef0eb38acdf313))
* share agent runtime config across clients ([e6ef828](https://github.com/ax-platform/ax-cli/commit/e6ef828d22131f4265d6b74d5ec430d6b722532e))
* **signals:** emit task app cards from CLI ([acf9b5e](https://github.com/ax-platform/ax-cli/commit/acf9b5e8a2bafcca54f0d48f4aa74e6f80ee83d5))
* support named user login environments ([3cbe6ab](https://github.com/ax-platform/ax-cli/commit/3cbe6ab771d9a36bf27ca38951b8de961fb39c78))


### Bug Fixes

* **apps:** hydrate collection signal payloads ([e3e236c](https://github.com/ax-platform/ax-cli/commit/e3e236c2b4cfe41aac993410c996a921b2ab356e))
* **apps:** hydrate whoami signal identity ([2fcbdfa](https://github.com/ax-platform/ax-cli/commit/2fcbdfa5071a6cc2cf49abe32bf6047ea36885f9))
* **apps:** include alert routing metadata ([5ee0bc0](https://github.com/ax-platform/ax-cli/commit/5ee0bc0d3078e694feaaa9feb609492ddcc2c789))
* **apps:** mark passive signals as signal-only ([b076d3f](https://github.com/ax-platform/ax-cli/commit/b076d3f820447422d7217101c73134b79f1cd787))
* bind cli listeners to explicit spaces ([0777b3c](https://github.com/ax-platform/ax-cli/commit/0777b3c89200f6c87270edc91ac779c3c6aad1b7))
* **channel:** process idle event before JWT reconnect + LISTENER-001 presence receipts docs ([#59](https://github.com/ax-platform/ax-cli/issues/59)) ([#61](https://github.com/ax-platform/ax-cli/issues/61)) ([641f1ea](https://github.com/ax-platform/ax-cli/commit/641f1ea75ce2099a7ed3e26f449c3c3e03ae3014))
* **cli:** clarify targeted send waits ([ef5edff](https://github.com/ax-platform/ax-cli/commit/ef5edffa1153a0caa50253e5c162278de3a77a16))
* **cli:** ignore unsafe local user-agent config ([46d6b3a](https://github.com/ax-platform/ax-cli/commit/46d6b3af2dafacd2a57d2c235d943c7702301049))
* **cli:** wake assigned task agents ([0aea13d](https://github.com/ax-platform/ax-cli/commit/0aea13de11fba5b827d6012341b3f12a6de8d48f))
* confirm hidden login token capture ([d392447](https://github.com/ax-platform/ax-cli/commit/d392447efdf3d456f5b9423778b5527bb96db7fd))
* keep user login distinct from agent identity ([591478b](https://github.com/ax-platform/ax-cli/commit/591478b66c941931a84285b073c6748c350a9067))
* **profile:** shell quote env exports ([5ec74c9](https://github.com/ax-platform/ax-cli/commit/5ec74c94550c5adb3bf8b355a8ce96e50642991c))
* **review:** clean up CLI auth and helper contracts ([305a3f9](https://github.com/ax-platform/ax-cli/commit/305a3f907b6cf1cc7e75b1fc8f2f97759a035884))
* scope cli message reads to spaces ([1dfed84](https://github.com/ax-platform/ax-cli/commit/1dfed848bf8dbc69095c9dc9e07098ab7af4d3bf))
* store user login separately from agent config ([e2a640f](https://github.com/ax-platform/ax-cli/commit/e2a640f81eba5fe680114a85d61e10442bee8d09))


### Documentation

* add login e2e and device approval flow ([6fdc9f5](https://github.com/ax-platform/ax-cli/commit/6fdc9f5277a3c65b1ce218f9ff15f5f7838a7be8))
* **auth:** clarify login bootstrap handoff ([4370ae7](https://github.com/ax-platform/ax-cli/commit/4370ae7e83c35efe6907ad43f710f07482b6b5b6))
* **auth:** standardize login handoff guidance ([2efc9e7](https://github.com/ax-platform/ax-cli/commit/2efc9e78e0884588c108f605c04cfad8dd556ac4))
* clarify current axctl bootstrap path ([3026a48](https://github.com/ax-platform/ax-cli/commit/3026a48cfc4e2648e2d1a2c7069288874923f6f9))
* clarify release automation posture ([02a6899](https://github.com/ax-platform/ax-cli/commit/02a6899d48cda61c8d32613ac4c76a23bcdb6b82))
* **cli:** clarify attachment and context upload paths ([f0f076c](https://github.com/ax-platform/ax-cli/commit/f0f076c50826fb1bccb254760a3fb70b5d788207))
* **cli:** document active handoff wait loop ([ce101fb](https://github.com/ax-platform/ax-cli/commit/ce101fbb516edf6230c24557879ea22dd71d7b0a))
* **cli:** lock in operator QA workflow ([76b37a7](https://github.com/ax-platform/ax-cli/commit/76b37a7ed7d611a227edd0d65876566a60592b3d))
* **cli:** teach bidirectional agent handoffs ([fc2d76d](https://github.com/ax-platform/ax-cli/commit/fc2d76dc8443720790a75afed3bcd24628d80d14))
* fix login e2e dev cli invocation ([662d7fb](https://github.com/ax-platform/ax-cli/commit/662d7fbfb736c29dcbf65be0241ef627d6e1e04d))
* **skill:** add coordination pattern guidance ([f6dce66](https://github.com/ax-platform/ax-cli/commit/f6dce66ee4c5ac4ce903443372bb1ea365913a95))

## [0.4.0](https://github.com/ax-platform/ax-cli/compare/v0.3.1...v0.4.0) (2026-04-13)


### Features

* **listen:** respect backend kill-switch in ax listen mention gate ([#29](https://github.com/ax-platform/ax-cli/issues/29)) ([ace5aa9](https://github.com/ax-platform/ax-cli/commit/ace5aa9caf2fcf6e86fb6739c23057349a2469e0))
* token mint, user guardrail, config cleanup + upload fixes ([#43](https://github.com/ax-platform/ax-cli/issues/43)) ([9df4182](https://github.com/ax-platform/ax-cli/commit/9df4182597f16b1d182f8135923d987326330f38))


### Bug Fixes

* **auth:** require agent-bound tokens for channel replies ([e98bb7d](https://github.com/ax-platform/ax-cli/commit/e98bb7d5246f4af5138e5ab47b57101c4ac269dd))
* **cli:** use exchanged JWT for SSE watch/events commands ([#18](https://github.com/ax-platform/ax-cli/issues/18)) ([f7e4750](https://github.com/ax-platform/ax-cli/commit/f7e4750a3714fcc40d4885cc239e10713c0684e7))
* credential audience option ([#37](https://github.com/ax-platform/ax-cli/issues/37)) ([2cad6f8](https://github.com/ax-platform/ax-cli/commit/2cad6f8b6da1bfe9eefbe607144224c98fba20f3))
* **listen:** kill switch gate drops mentions instead of deferring ([#28](https://github.com/ax-platform/ax-cli/issues/28)) ([f0bb9a2](https://github.com/ax-platform/ax-cli/commit/f0bb9a22643730915c685c032fe311815185f620))
* **listen:** trust backend mentions array instead of content regex ([#31](https://github.com/ax-platform/ax-cli/issues/31)) ([f1be831](https://github.com/ax-platform/ax-cli/commit/f1be8315eee6ea52053fbd35869e0b39d858b1be))
* make context uploads and downloads space-safe ([#36](https://github.com/ax-platform/ax-cli/issues/36)) ([7bc60c5](https://github.com/ax-platform/ax-cli/commit/7bc60c54dceb8cdcc4740d37d9cb2e7435aecb49))
* resolve overlapping elements in profile fingerprint SVG ([#33](https://github.com/ax-platform/ax-cli/issues/33)) ([629e61f](https://github.com/ax-platform/ax-cli/commit/629e61fc95b290dc7f34f66e2dc0d92af07b02cc))


### Documentation

* add MCP docs (headless PAT + remote OAuth 2.1); remove internal files ([#38](https://github.com/ax-platform/ax-cli/issues/38)) ([dfd1d99](https://github.com/ax-platform/ax-cli/commit/dfd1d9937b49a6e2f66a872cb81f5a965fdb576c))
* **examples:** add runnable hermes_sentinel integration example ([#27](https://github.com/ax-platform/ax-cli/issues/27)) ([914d9fe](https://github.com/ax-platform/ax-cli/commit/914d9fed5115a4ecba0bbc5506f6a440d7330911))
* land AX-SCHEDULE-001 spec + remove CIPHER_TEST cruft ([#26](https://github.com/ax-platform/ax-cli/issues/26)) ([0ab335b](https://github.com/ax-platform/ax-cli/commit/0ab335b0a31692a89f6f5f1cf2dc6b67b11f7ea7))
* scrub internal agent names from README ([#34](https://github.com/ax-platform/ax-cli/issues/34)) ([ba6f3b2](https://github.com/ax-platform/ax-cli/commit/ba6f3b20035e8795848f2bfd8f9e60405409ebdf))
* update README for new user onboarding ([#32](https://github.com/ax-platform/ax-cli/issues/32)) ([1d38e01](https://github.com/ax-platform/ax-cli/commit/1d38e01bab16e7baddbb7f69c2156e9be589fb6c))

## Changelog

All notable changes to `axctl` are tracked here.

This project uses [Conventional Commits](https://www.conventionalcommits.org/)
and Release Please to generate release PRs, version bumps, and changelog entries.
