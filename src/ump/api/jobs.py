from sqlalchemy import select
from sqlalchemy.orm import Session

from ump import utils
from ump.api.db_handler import DBHandler
from ump.api.db_handler import db_engine as engine
from ump.api.job_queries import build_job_filter
from ump.api.models.ensemble import Ensemble, JobsEnsembles
from ump.api.models.job import Job
from ump.config import app_settings

def append_ensemble_list(job):
    with Session(engine) as session:
        stmt = select(JobsEnsembles).where(JobsEnsembles.job_id == job['jobID'])
        ids = session.scalars(stmt).fetchall()
        results = []
        for id_pair in ids:
            results.append(id_pair.ensemble_id)
        stmt = select(Ensemble).where(Ensemble.id.in_(results))
        ensembles = session.scalars(stmt).fetchall()
        job['ensembles'] = []
        for ensemble in ensembles:
            job['ensembles'].append(ensemble.to_dict())

def get_jobs(args, auth_token=None):
    """List the jobs the authenticated user owns or that were shared with them.

    Anonymous callers get an empty list (see issue #24): anonymous jobs are
    not tied to a session, so listing them would expose every anonymous job
    to everyone. They remain reachable by their job ID.
    """
    page  = int(args["page"][0]) if "page" in args else 1
    limit = int(args["limit"][0]) if "limit" in args else None

    if auth_token is None:
        return {"jobs": [], "links": [], "total_count": 0}

    user = auth_token["sub"]
    jobs = []
    query = "SELECT j.job_id FROM jobs j"
    conditions, query_params = build_job_filter(args, user)

    with DBHandler() as db:
        db.set_sortable_columns(Job.SORTABLE_COLUMNS)

        job_ids = db.run_query(query,
            conditions   = conditions,
            query_params = query_params,
            order        = ['created'],
            limit        = limit,
            page         = page
        )

    for row in job_ids:
        job = Job(row['job_id'], user)
        jobs.append(job.display())

    count_jobs = count(conditions, query_params)
    links = next_links(page, limit, count_jobs)

    return { "jobs": jobs, "links": links, "total_count": count_jobs }

def next_links(page, limit, count_jobs):
    if not limit or count_jobs <= limit:
        return []

    links = []
    if count_jobs > (page - 1) * limit:
        links.append({
            "href": utils.join_url_parts(
                app_settings.UMP_API_SERVER_URL,
                app_settings.UMP_API_SERVER_URL_PREFIX,
                f"jobs?page={page+1}&limit={limit}"
            ),
            "rel": "service",
            "type": "application/json",
            "hreflang": "en",
            "title": "Next page of jobs."
        })

    if page > 1:
        links.append({
            "href": utils.join_url_parts(
                app_settings.UMP_API_SERVER_URL,
                app_settings.UMP_API_SERVER_URL_PREFIX,
                f"jobs?page={page-1}&limit={limit}"
            ),
            "rel": "service",
            "type": "application/json",
            "hreflang": "en",
            "title": "Previous page of jobs."
        })

    return links

def count(conditions, query_params):
    count_query = "SELECT count(*) FROM jobs j"
    with DBHandler() as db:
        count_jobs = db.run_query(
            count_query,
            conditions=conditions,
            query_params=query_params
        )
    return count_jobs[0]['count']
