import random
from typing import Any, Dict


def sample_company_context(rng: random.Random) -> Dict[str, Any]:
    industries = [
        "financial services", "healthcare", "engineering",
        "retail", "software", "logistics",
    ]
    return {
        "industry": rng.choice(industries),
    }


def sample_employee_persona(rng: random.Random) -> Dict[str, str]:
    departments = ["IT", "Operations", "Customer Support", "Sales", "Finance", "HR", "Product", "Engineering"]
    seniority = ["junior", "medior", "senior", "lead", "manager"]

    role_titles_by_dept = {
        "IT": ["Systems Engineer", "Cloud Engineer", "Service Desk Analyst", "Security Analyst"],
        "Operations": ["Operations Coordinator", "Process Specialist", "Planner"],
        "Customer Support": ["Support Agent", "Customer Success Specialist", "Team Lead Support"],
        "Sales": ["Account Executive", "Sales Operations Specialist", "Key Account Manager"],
        "Finance": ["Financial Analyst", "Controller", "Accounts Payable Specialist"],
        "HR": ["HR Advisor", "Recruiter", "People Operations Specialist"],
        "Product": ["Product Owner", "Business Analyst", "Product Manager"],
        "Engineering": ["Software Engineer", "QA Engineer", "Engineering Manager"],
    }

    dept = rng.choice(departments)
    return {
        "department": dept,
        "role_title": rng.choice(role_titles_by_dept[dept]),
        "seniority": rng.choice(seniority),
    }
