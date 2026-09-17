# TEST SEAMS

Every production/test seam must have a production-default assertion and a
mutation proof. Test isolation is injected with environment variables or DI;
production code never detects pytest or test names.

Each row records (a) the production-default assertion and (b) the mutation
proof.

| ID | Seam | Production default | Assertion | Mutation proof |
|---|---|---|---|---|
| S1： | MTS state path | `mts_position_state_path()` | `test_mts_default_is_runtime_authority` | `test_mutation_tmp_state_path_is_rejected` |
| S2： | test-name detection | absent | `test_production_has_no_test_runtime_detection_or_tmp_state` | `test_mutation_pytest_detection_is_rejected` |
| S3： | pytest module detection | absent | same structural test | `test_mutation_pytest_detection_is_rejected` |
| S4： | safety status path | runtime state root | structural path test | tmp state mutation rejected |
| S5： | fill/event logs | runtime logs | runtime-path contract tests | path mutation scan |
| S6： | test environment injection | explicit env/fixture | test conftest injection | no production branch |
| S7： | dashboard date filter | requested date | dashboard contract tests | `if True` mutation rejected |
| S8： | other injectable envs | explicit env/DI | module contract tests | path/branch scan |

The register itself is CI-checked; removing an entry is a failing mutation.
