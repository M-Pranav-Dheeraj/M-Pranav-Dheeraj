"""Free, local job-matching engine for JobPilot AI.

No external AI/API is required. The matcher combines normalized skill aliases,
phrases, role/title relevance, seniority compatibility, location/work-mode
signals and resume evidence. It returns an explainable score.
"""
import re
from datetime import datetime, timezone

ALIASES = {
    "python": {"python", "python3"},
    "sql": {"sql", "mysql", "postgresql", "postgres", "ms sql", "sql server"},
    "snowflake": {"snowflake", "snowflake data warehouse"},
    "power bi": {"power bi", "powerbi"},
    "machine learning": {"machine learning", "ml", "machine-learning"},
    "deep learning": {"deep learning", "dl"},
    "scikit learn": {"scikit-learn", "scikit learn", "sklearn"},
    "pandas": {"pandas"},
    "numpy": {"numpy"},
    "tensorflow": {"tensorflow", "keras"},
    "pytorch": {"pytorch", "torch"},
    "aws": {"aws", "amazon web services"},
    "azure": {"azure", "microsoft azure"},
    "gcp": {"gcp", "google cloud", "google cloud platform"},
    "c++": {"c++", "cpp"},
    "c#": {"c#", "csharp", "c sharp"},
    ".net": {".net", "dotnet", "asp.net", "aspnet", ".net core", "dot net"},
    "rest api": {"rest api", "restful api", "restful services", "web api"},
    "fastapi": {"fastapi"},
    "django": {"django"},
    "flask": {"flask"},
    "react": {"react", "react.js", "reactjs"},
    "angular": {"angular", "angularjs"},
    "java": {"java"},
    "git": {"git", "github", "gitlab"},
    "docker": {"docker", "containerization"},
    "spark": {"spark", "apache spark", "pyspark"},
    "airflow": {"airflow", "apache airflow"},
    "etl": {"etl", "extract transform load", "data pipeline", "data pipelines"},
    "data engineering": {"data engineering", "data engineer", "data platform"},
    "data analysis": {"data analysis", "data analytics", "data analyst", "analytics"},
    "artificial intelligence": {"artificial intelligence", "ai", "ai/ml", "ai ml"},
    "nlp": {"nlp", "natural language processing"},
}

ROLE_ALIASES = {
    "data engineer": {"data engineer", "data engineering", "etl developer", "data platform engineer", "analytics engineer"},
    "data analyst": {"data analyst", "data analysis", "business analyst", "reporting analyst", "bi analyst", "analytics analyst"},
    "ai/ml engineer": {"ai/ml engineer", "ai ml engineer", "machine learning engineer", "ml engineer", "ai engineer", "artificial intelligence engineer"},
    "software engineer": {"software engineer", "software developer", "application developer", "backend developer", "full stack developer", "developer"},
    "python developer": {"python developer", "python engineer", "backend python developer"},
}

SENIOR_TERMS = {
    "intern": 0, "internship": 0, "trainee": 0, "fresher": 0, "entry level": 0,
    "junior": 1, "associate": 1, "graduate": 1, "engineer i": 1,
    "mid": 2, "mid-level": 2, "intermediate": 2,
    "senior": 3, "sr": 3, "lead": 4, "principal": 5, "staff": 5,
    "architect": 5, "manager": 5, "director": 6, "head": 6,
}

def normalize(text):
    s=(text or "").lower()
    s=s.replace("&", " and ")
    s=re.sub(r"[^a-z0-9+#.\-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def phrases(text):
    s=normalize(text)
    padded=" "+s+" "
    found=set()
    for canonical, variants in ALIASES.items():
        if any(" "+normalize(v)+" " in padded for v in variants):
            found.add(canonical)
    return found

def role_phrases(text):
    s=normalize(text)
    padded=" "+s+" "
    found=set()
    for canonical, variants in ROLE_ALIASES.items():
        if any(" "+normalize(v)+" " in padded for v in variants):
            found.add(canonical)
    return found

def requested_skills(profile):
    raw=[x.strip() for x in (profile.get("skills") or "").split(",") if x.strip()]
    out=set()
    for item in raw:
        n=normalize(item)
        out.add(n)
        for canonical, variants in ALIASES.items():
            if n == canonical or n in {normalize(v) for v in variants}:
                out.add(canonical)
    return out

def requested_roles(profile):
    raw=[x.strip() for x in (profile.get("roles") or "").split(",") if x.strip()]
    out=set()
    for item in raw:
        n=normalize(item)
        out.add(n)
        for canonical, variants in ROLE_ALIASES.items():
            if n == canonical or n in {normalize(v) for v in variants}:
                out.add(canonical)
    return out

def seniority(text):
    s=normalize(text)
    # Longer phrases first prevents "senior" from winning inside a phrase.
    for term, level in sorted(SENIOR_TERMS.items(), key=lambda x:-len(x[0])):
        if re.search(r"\b"+re.escape(term)+r"\b", s):
            return level
    return 1

def experience_level(profile):
    s=normalize(profile.get("experience") or "")
    if any(x in s for x in ("fresher","0 year","0-year","entry")): return 0
    nums=re.findall(r"\d+(?:\.\d+)?",s)
    if nums:
        try:
            y=float(nums[0])
            return 1 if y<=1 else 2 if y<=3 else 3 if y<=6 else 5
        except ValueError: pass
    return 1

def location_match(job_location, preferred):
    loc=normalize(job_location)
    prefs=[normalize(x) for x in (preferred or "").split(",") if normalize(x)]
    if not prefs: return 0
    if any(p in loc for p in prefs): return 1
    if "remote" in prefs and any(x in loc for x in ("remote","anywhere","worldwide")): return 1
    if "india" in prefs and ("india" in loc or "remote" in loc): return 1
    return 0

def freshness_bonus(job):
    raw=job.get("posted_at") or job.get("created_at") or ""
    if not raw: return 0
    try:
        s=str(raw).replace("Z","+00:00")
        dt=datetime.fromisoformat(s)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        days=max(0,(datetime.now(timezone.utc)-dt).total_seconds()/86400)
        if days <= 1: return 5
        if days <= 3: return 4
        if days <= 7: return 3
        if days <= 14: return 1
    except Exception:
        pass
    return 0

def match_job(job, profile):
    title=normalize(job.get("title"))
    body=normalize(" ".join(str(job.get(k,"")) for k in ("title","company","location","description")))
    requested=requested_skills(profile)
    available=phrases(body)
    matched=sorted(requested & available)
    missing=sorted(requested - available)
    roles=requested_roles(profile)
    job_roles=role_phrases(title+" "+body)
    role_hit=bool(roles & job_roles) or any(r in title for r in roles if len(r)>3)

    # Phrase-level skill compatibility is the dominant signal.
    skill_ratio=len(matched)/max(1,len(requested))
    role_score=1 if role_hit else 0
    title_role_bonus=1 if any(r in title for r in roles if len(r)>3) else 0

    exp_level=experience_level(profile)
    job_level=seniority(title)
    seniority_penalty=0
    if exp_level <= 1 and job_level >= 4: seniority_penalty=15
    elif exp_level <= 1 and job_level == 3: seniority_penalty=8

    loc=location_match(job.get("location",""),profile.get("locations",""))
    work_pref=normalize(profile.get("work_mode") or "any")
    job_loc=normalize(job.get("location"))
    work=1 if work_pref in ("any","") else int(work_pref in job_loc or (work_pref=="remote" and "remote" in body))

    resume=normalize(profile.get("resume_text") or "")
    resume_skills=phrases(resume)
    resume_ratio=len(matched & resume_skills)/max(1,len(matched)) if matched else 0

    # Base: 40 skills, 25 role, 12 title, 8 resume, 7 location, 3 work mode, 5 freshness.
    raw=(40*skill_ratio)+(25*role_score)+(12*title_role_bonus)+(8*resume_ratio)+(7*loc)+(3*work)+freshness_bonus(job)-seniority_penalty
    score=round(max(0,min(100,raw)),1)

    reasons=[]
    if matched:
        reasons.append("Matched skills: "+", ".join(matched[:6]))
    if title_role_bonus:
        reasons.append("Target role appears in the job title")
    elif role_hit:
        reasons.append("Responsibilities align with a target role")
    if resume_ratio >= .5 and matched:
        reasons.append("Resume skills support the match")
    if loc:
        reasons.append("Location preference matches")
    if work:
        reasons.append("Work-mode preference is compatible")
    if seniority_penalty:
        reasons.append("Senior-level requirement may be above your experience level")
    if not reasons:
        reasons.append("Limited profile evidence; review the posting carefully")

    return {
        "score": score,
        "matched_skills": matched[:12],
        "missing_skills": missing[:12],
        "reasons": reasons[:5],
        "seniority_penalty": seniority_penalty,
    }
