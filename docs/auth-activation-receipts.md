# Account activation receipt failures

The account transaction may apply the selected account and activate its process
before the durable activation receipt is saved. If that final receipt guard or
write fails, the account result stays unsuccessful and keeps the existing
instruction to inspect the terminal before retrying. The selected account is not
silently rolled back or retried because that would repeat an applied action.

Failed web account results may carry `activation_receipt_failure`. Successful
results omit it. This optional diagnostic has exactly six fields:

- `schema`: `t1000.auth-activation-receipt-failure.v1`.
- `phase`: `record-activation`.
- `reason`: a closed lifecycle, process, credential, receipt-path, I/O or unknown
  classification. No exception text or arbitrary nested object is accepted.
- `errno`: a known integer OS error number when actually observed, otherwise null.
- `lifecycle_kind`: stopped, idle-shell, codex-restart or unknown.
- `candidate_pid`: an observed positive integer candidate PID, otherwise null.

The diagnostic carries no credential, account payload, prompt, environment,
command line, process output or private file path. Classification uses builtin
exception descriptors and exact known messages. Optional telemetry faults derived
from Exception preserve the original unsuccessful result and its publication;
BaseException still propagates. The diagnostic never grants account authority or
changes process, credential, readiness, replay, rollback or launch admission.

A retained generic refusal does not establish which receipt check or write failed.
Fixtures that exercise each classification do not reproduce an earlier live
failure or prove a provider cause. Account history/resume acceptance still needs
its independent exact runtime evidence.
