// Tunable parameters for infra/main.bicep. `acrName` and `containerImage` are computed at
// deploy time; deploy.ps1 overrides these placeholders on the CLI
// (`--parameters acrName=<name> containerImage=<ref>`), which take precedence over the file.
using './main.bicep'

param acrName = '<set-by-deploy.ps1>'
param containerImage = '<set-by-deploy.ps1>'
param namePrefix = 'nightingale'
// Latency knobs so the deployed mocks show realistic timing in the Control Plane traces.
param mockLatencyMs = '250'
param mockJitterMs = '150'
param toolTimeoutMs = '3000'
