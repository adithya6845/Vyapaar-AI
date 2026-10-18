# Inbox Fraud Guard - datasets (SYNTHETIC)
All bank accounts, UPI IDs, GSTINs, phones, domains and emails are fake, generated to link to your existing SupplierIDs and PO_IDs.
- suppliers_v2.csv: your suppliers plus SupplierDomain, AccountsEmail (company-domain emails replace the gmail/hotmail ones).
- supplier_payment_profiles.csv: verified bank/UPI/GSTIN per supplier (ground truth for "did bank details change?").
- supplier_payment_history.csv: 623 payments made against POs up to 2025-06-30 (177 POs still open).
- incoming_emails.csv: 100 labeled emails (60 Legit, 40 Fraud) dated 2025-07-01 to 07-21, with Label, ScamType, ExpectedFlags, ExpectedAction (PASS / REVIEW / HOLD).
- known_bad_indicators.csv: blocklist of scam domains, accounts, UPIs, URLs, phones (covers only about half the fraud emails on purpose).
Note: 3 legit emails announce a genuine bank change; they should be REVIEW (callback verification), never auto-PASS.
