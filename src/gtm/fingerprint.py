"""Frozen inputs for import fingerprints.

File and row keys were originally derived from the YAML header mapping. Freezing the
mapping that produced every existing key means config/gtm.yaml can be cleaned up or
extended without making already-imported files look new (which would re-import them
and flood the review queue). Import keys depend only on file content, source, and
folder segment. Never edit this mapping.
"""

FINGERPRINT_VERSION = "local-csv-v1"

LEGACY_MAPPINGS = {
    "grok": {
        "full_name": ["full_name", "name", "contact name"],
        "email": ["email", "work email"],
        "email_verified": ["email_verified", "verified"],
        "linkedin_url": ["linkedin_url", "linkedin", "linkedin profile url"],
        "company_name": ["company_name", "company", "account"],
        "domain": ["domain", "company domain"],
        "domain_verified": ["domain_verified", "company domain verified"],
        "website": ["website", "company website", "company_website"],
        "job_title": ["job_title", "title", "job title"],
        "discovered_at": ["discovered_at", "discovery date"],
        "external_id": ["external_id", "id"],
        "objective": ["objective", "campaign"],
        "create_opportunity": ["create_opportunity"],
        "opportunity_stage": ["opportunity_stage"],
        "icp_segment": ["icp_segment", "segment"],
        "notes": ["notes"],
    },
    "linkedin": {
        "full_name": ["full_name", "name", "full name"],
        "email": ["email", "email address"],
        "email_verified": ["email_verified"],
        "linkedin_url": ["linkedin_url", "url", "linkedin profile url"],
        "company_name": ["company_name", "company"],
        "domain": ["domain", "company domain"],
        "domain_verified": ["domain_verified", "company domain verified"],
        "website": ["website", "company website", "company_website"],
        "job_title": ["job_title", "position", "job title"],
        "discovered_at": ["discovered_at", "discovery date"],
        "connection_status": ["connection_status", "status"],
        "connection_request_at": ["connection_request_at", "connection request date"],
        "last_contact_at": ["last_contact_at", "last contact date"],
        "next_followup_at": ["next_followup_at", "next follow-up date"],
        "objective": ["objective", "campaign"],
        "create_opportunity": ["create_opportunity"],
        "opportunity_stage": ["opportunity_stage"],
        "icp_segment": ["icp_segment", "segment"],
        "notes": ["notes"],
    },
}
