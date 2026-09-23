#!/usr/bin/env bash
# Copyright IBM Corp. 2014, 2025
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly target="internal/acceptance/testcase.go"
patch_file=""
patch_applied=false

function cleanup {
  exit_code=$?

  if [[ "$patch_applied" == true ]]; then
    if ! git apply --reverse "$patch_file"; then
      echo "error: could not remove the temporary change from $target" >&2
      exit 1
    fi
  fi

  [[ -z "$patch_file" ]] || rm -f "$patch_file"
  exit "$exit_code"
}

function usage {
  echo "Usage: $0 <provider-version> [--] <go-test-command> [args...]" >&2
  echo "Example: $0 4.77.0 -- go test -v ./internal/services/resource -run TestAccResourceGroup_basic -timeout 60m" >&2
}

function write_patch {
  patch_file=$(mktemp)
  cat > "$patch_file" <<'PATCH'
diff --git a/internal/acceptance/testcase.go b/internal/acceptance/testcase.go
--- a/internal/acceptance/testcase.go
+++ b/internal/acceptance/testcase.go
@@ -7,6 +7,7 @@ import (
 	"context"
 	"fmt"
 	"os"
+	"strings"
 	"testing"
 
 	"github.com/hashicorp/go-version"
@@ -21,6 +22,11 @@ import (
 	"github.com/hashicorp/terraform-provider-azurerm/internal/vcr"
 )
 
+const (
+	providerName    = "azurerm"
+	altProviderName = "azurerm-alt"
+)
+
 func (td TestData) DataSourceTest(t *testing.T, steps []TestStep) {
 	// DataSources don't need a check destroy - however since this is a wrapper function
 	// and not matching the ignore pattern `XXX_data_source_test.go`, this needs to be explicitly opted out
@@ -199,6 +205,7 @@ func (td TestData) runAcceptanceTest(t *testing.T, testCase resource.TestCase) {
 
 	testCase.ExternalProviders = td.externalProviders()
 	testCase.ProtoV5ProviderFactories = framework.ProtoV5ProviderFactoriesInitWithTestName(context.Background(), t.Name(), "azurerm", "azurerm-alt")
+	td.modifyTestCase(&testCase)
 
 	resource.ParallelTest(t, testCase)
 }
@@ -212,6 +219,7 @@ func (td TestData) runAcceptanceSequentialTest(t *testing.T, testCase resource.T
 
 	testCase.ExternalProviders = td.externalProviders()
 	testCase.ProtoV5ProviderFactories = framework.ProtoV5ProviderFactoriesInitWithTestName(context.Background(), t.Name(), "azurerm")
+	td.modifyTestCase(&testCase)
 
 	resource.Test(t, testCase)
 }
@@ -236,3 +244,109 @@ func (td TestData) externalProviders() map[string]resource.ExternalProvider {
 		},
 	}
 }
+
+// modifyTestCase rewrites a TestCase for breaking change detection when TF_ACC_PROVIDER_VERSION is
+// set: the configuration is applied by the released provider from the registry and then imported and
+// verified by the locally built provider, so any state incompatibility surfaces as an import diff.
+func (td TestData) modifyTestCase(testCase *resource.TestCase) {
+	providerVersion := os.Getenv("TF_ACC_PROVIDER_VERSION")
+	if providerVersion == "" {
+		return
+	}
+
+	for _, step := range testCase.Steps {
+		// `azurerm-alt` has no registry address of its own, so it cannot be pinned to a release.
+		if strings.Contains(step.Config, altProviderName) {
+			return
+		}
+	}
+
+	// Steps only opt out of the TestCase level factories when those are unset, since the framework
+	// merges the TestCase level factories into every step.
+	localFactories := testCase.ProtoV5ProviderFactories
+	localExternalProviders := testCase.ExternalProviders
+	testCase.ProtoV5ProviderFactories = nil
+	testCase.ExternalProviders = nil
+
+	releasedExternalProviders := make(map[string]resource.ExternalProvider, len(localExternalProviders)+1)
+	for name, provider := range localExternalProviders {
+		releasedExternalProviders[name] = provider
+	}
+	releasedExternalProviders[providerName] = resource.ExternalProvider{
+		VersionConstraint: fmt.Sprintf("=%s", strings.TrimPrefix(providerVersion, "v")),
+		Source:            "registry.terraform.io/hashicorp/azurerm",
+	}
+
+	importStateVerifyIgnore := make([]string, 0)
+	for _, step := range testCase.Steps {
+		if step.ImportState {
+			importStateVerifyIgnore = append(importStateVerifyIgnore, step.ImportStateVerifyIgnore...)
+		}
+	}
+
+	// Refreshing with the local provider would rewrite the state written by the released provider and
+	// mask the differences the import step is looking for.
+	configSteps := make([]TestStep, 0, len(testCase.Steps))
+	for _, step := range testCase.Steps {
+		if step.RefreshState {
+			continue
+		}
+		configSteps = append(configSteps, step)
+	}
+
+	steps := make([]TestStep, 0, len(configSteps)*2)
+	for i, step := range configSteps {
+		steps = append(steps, step)
+
+		if td.requiresGeneratedImportStep(configSteps, i) {
+			steps = append(steps, TestStep{
+				ResourceName:            td.ResourceName,
+				ImportState:             true,
+				ImportStateVerify:       true,
+				ImportStateVerifyIgnore: importStateVerifyIgnore,
+			})
+		}
+	}
+
+	lastConfig := ""
+	for i := range steps {
+		if steps[i].Config != "" {
+			lastConfig = steps[i].Config
+			steps[i].ExternalProviders = releasedExternalProviders
+			continue
+		}
+
+		steps[i].ProtoV5ProviderFactories = localFactories
+		steps[i].ExternalProviders = localExternalProviders
+	}
+
+	// Declaring providers on a step makes the framework replace the working directory config with a
+	// provider-only one, and only apply steps write the real config back, so the run has to end on an
+	// apply step for the post-test destroy to find a configured `azurerm` provider.
+	if lastConfig != "" {
+		steps = append(steps, TestStep{
+			Config:                   lastConfig,
+			ProtoV5ProviderFactories: localFactories,
+			ExternalProviders:        localExternalProviders,
+		})
+	}
+
+	testCase.Steps = steps
+}
+
+func (td TestData) requiresGeneratedImportStep(steps []TestStep, index int) bool {
+	if strings.HasPrefix(td.ResourceName, "data.") {
+		return false
+	}
+
+	step := steps[index]
+	if step.Config == "" || step.Destroy || step.PlanOnly || step.ExpectNonEmptyPlan || step.ExpectError != nil {
+		return false
+	}
+
+	if index+1 < len(steps) {
+		return !steps[index+1].ImportState
+	}
+
+	return true
+}
PATCH
}

function main {
  if [[ $# -lt 2 ]]; then
    usage
    exit 2
  fi

  provider_version=$1
  shift
  if [[ "${1:-}" == "--" ]]; then
    shift
  fi
  if [[ $# -eq 0 ]]; then
    usage
    exit 2
  fi

  repo_root=$(git rev-parse --show-toplevel)
  cd "$repo_root"

  if ! git diff --quiet -- "$target" || ! git diff --cached --quiet -- "$target"; then
    echo "error: $target has local changes; refusing to overwrite them" >&2
    exit 1
  fi

  write_patch
  if ! git apply --check "$patch_file"; then
    echo "error: temporary patch does not apply to this checkout" >&2
    exit 1
  fi

  git apply "$patch_file"
  patch_applied=true

  TF_ACC=1 TF_ACC_PROVIDER_VERSION="$provider_version" "$@"
}

trap cleanup EXIT
main "$@"