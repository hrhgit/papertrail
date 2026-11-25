# relay

Delivers webhook events to customer endpoints. Each event is signed, sent with
a POST, and retried when the endpoint does not accept it.

| Setting | Value | Meaning |
| --- | --- | --- |
| `MAX_ATTEMPTS` | 3 | Sends per event before it is parked |
| `TIMEOUT_SECONDS` | 30 | Wait for the endpoint to answer |

## For receivers

A request that times out on our side may still have reached you, and we will send
it again. Every attempt for one event carries the same `Idempotency-Key` header.
Store the key and ignore an event you have already processed.

## Run the tests

    python -m pytest

Owner: payments platform team.
