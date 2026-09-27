## Create a dependency-ordered plan for the approved requirement. Include risks, validation, and rollback.

### Result
Prepared a reviewable, scoped output using the supplied requirement and repository context.

### Validation notes
No external model or network access was used. A human must review generated changes before release.

### Context
Requirement: Add analytics to the URL shortener.
Acceptance: ['Preserve current behavior except for the requested change', 'Validate the relevant API and data-flow behavior with tests', 'Produce reviewable changes and evidence before release readiness']
Inspection paths: ['src/agent_runtime/__init__.py', 'src/agent_runtime/providers.py', 'src/agent_runtime/sandbox.py', 'src/agent_runtime/scenarios.py', 'src/agentic_se/__init__.py', 'src/agentic_se/__main__.py', 'src/url_shortener/__init__.py', 'src/url_shortener/http.py', 'src/url_shortener/repository.py', 'src/url_shortener/service.py', 'src/workflow_engine/__init__.py', 'src/workflow_engine/engine.py', 'src/workflow_engine/runtime.py', 'src/workflow_engine/store.py', 'tests/test_engine.py', 'tests/test_runtime.py', 'tests/test_scenarios.py', 'tests/test_shortener.py']
Resolution: Track total and daily click counts; no personal identifiers; protect analytics with an API key.
