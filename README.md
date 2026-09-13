# wiki-substrate

Permissioned, versioned markdown tree served as a remote MCP server. The design
is `HANDOFF.md`; read §1, §2, §4, §11 first.

    make sync      # deps
    make test      # unit tests, no AWS
    make synth     # cdk synth (builds Lambda packages first)
    make deploy ENV=dev

Deployment needs: an AWS dev account with credentials configured, the WorkOS
staging environment passing the §2 gate, and `infra/config.py` filled in.
