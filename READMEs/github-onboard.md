# Black Duck onboarding runbook

## 1. Configure the target

Set the target repository to `required`, exclude the workflow repository, and enable ruleset automation.

```toml
[policy]
active_known = "required"
inactive_known = "review"
unknown = "review"
fork = "review"

[[policy.repository_overrides]]
repository = "CUSTOMER_ORG/TARGET_REPO"
result = "required"
reason = "Black Duck SCA target."

[[policy.repository_overrides]]
repository = "CUSTOMER_ORG/blackduck-workflows"
result = "excluded"
reason = "Central workflow repository."

[workflow]
enabled = true
source_repository = "CUSTOMER_ORG/blackduck-workflows"
local_path = "workflows/blackduck-required.yml"

[ruleset]
enabled = true
name = "Black Duck SCA Required"
enforcement = "evaluate"
include_policy_value = "required"
```

## 2. Publish the workflow

```zsh
export GITHUB_ORG='CUSTOMER_ORG'
read -rs 'GITHUB_TOKEN?GitHub token: '
print
export GITHUB_TOKEN

github-onboard workflow --insecure
github-onboard workflow --insecure --apply
```

Confirm **Black Duck SCA** passes in the `blackduck-workflows` repository.

## 3. Configure Black Duck credentials

Under **Organization Settings → Secrets and variables → Actions**, create:

- Variable: `BLACKDUCK_URL`
- Secret: `BLACKDUCK_API_TOKEN`

Grant the target repository access.

## 4. Apply the custom properties

Explicitly scope the operation to the target and workflow repositories.

```zsh
export TARGET_REPOSITORY="${GITHUB_ORG}/TARGET_REPO"
export WORKFLOW_REPOSITORY="${GITHUB_ORG}/blackduck-workflows"

github-onboard properties \
  --limit 2 \
  --repository "${TARGET_REPOSITORY}" \
  --repository "${WORKFLOW_REPOSITORY}" \
  --refresh-all \
  --insecure

github-onboard properties \
  --limit 2 \
  --repository "${TARGET_REPOSITORY}" \
  --repository "${WORKFLOW_REPOSITORY}" \
  --refresh-all \
  --insecure \
  --apply
```

Confirm:

```text
TARGET_REPO: blackduck_sca_policy = required
blackduck-workflows: blackduck_sca_policy = excluded
```

## 5. Create the evaluated ruleset

Preview the exact target repositories:

```zsh
github-onboard rulesets --insecure
```

Create or update the ruleset in **Evaluate** mode:

```zsh
github-onboard rulesets --insecure --apply
```

The automation creates only:

- Custom-property targeting: `blackduck_sca_policy=required`
- Default-branch targeting
- The central required Black Duck workflow
- No bypass actors or unrelated rules

## 6. Verify and activate

1. Open a pull request in the target repository.
2. Confirm **Black Duck SCA** runs successfully.
3. Confirm unrelated repositories are not targeted.

Preview activation:

```zsh
github-onboard rulesets activate --insecure
```

Activate after review:

```zsh
github-onboard rulesets activate --insecure --apply
```

> `--insecure` is only for intercepted corporate TLS. Ruleset API access and enforcement require the appropriate GitHub plan. If GitHub returns an upgrade message, stop and use a Team or Enterprise organization.