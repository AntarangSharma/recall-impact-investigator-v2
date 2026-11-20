# Implementation plan

1. Inspect the CPSC guide, recall pages, and API descriptions. Define the source boundary and freeze the Insignia notice and its cases as the holdout.
2. Build the CSV reader, source versions, rule validator, and three-state matcher. Freeze manually specified expected outcomes and evidence.
3. Run the Secura three-unit workflow through the API and database, including restart during the identifier request.
4. Wire the evidence-driven graph, model adapter, review UI, and evaluation. Run fixture and live checks independently.
5. Capture the working UI, document measured results, verify a clean checkout and container, and publish to the authorized destination.
