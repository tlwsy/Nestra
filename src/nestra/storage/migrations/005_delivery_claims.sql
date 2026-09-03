-- Cross-process lease prevents duplicate external sends while network I/O is in flight.
ALTER TABLE deliveries ADD COLUMN claim_token TEXT;
ALTER TABLE deliveries ADD COLUMN claim_until TEXT;
CREATE INDEX idx_deliveries_claim ON deliveries(status, claim_until);
