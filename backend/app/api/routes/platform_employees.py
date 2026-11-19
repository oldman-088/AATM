"""Employees: role templates, dynamic hiring, skill gaps, specialists, replacement."""


from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.db.session import get_db
from app.employees.catalog import (
    ROLE_TEMPLATES,
    analyze_team_skill_gaps,
    create_dynamic_employee,
    provision_missing_specialists,
)
from app.models import (
    User,
)
from app.orchestration.replacement import NoFreeWorkerError, replacement_service
from app.schemas.common import (
    EmployeeCreate,
    EmployeeSkillGapRequest,
)

router = APIRouter(tags=["enterprise-platform"], dependencies=[Depends(get_current_user)])


@router.get("/employees/templates")
async def list_role_templates():
    return ROLE_TEMPLATES


@router.post("/employees", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_employee_endpoint(
    body: EmployeeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    emp = await create_dynamic_employee(
        db=db,
        name=body.name,
        role_template_id=body.role_template_id,
        specialty=body.specialty,
        skills=body.skills,
        divisions=body.divisions,
        permissions=body.permissions,
        provider_id=body.provider_id,
        model_name=body.model_name,
        allocation_mode=body.allocation_mode,
        preferred_account_id=body.preferred_account_id,
        create_browser_profile=body.create_browser_profile,
        autonomy_mode=body.autonomy_mode,
        device_id=body.device_id,
        actor=user.email,
    )
    if emp.role != "manager":
        emp.reports_to = body.reports_to or "manager-0"
    await db.commit()
    await db.refresh(emp)
    return {
        "id": emp.id,
        "name": emp.name,
        "role": emp.role,
        "role_template": emp.role_template,
        "reports_to": emp.reports_to,
        "specialty": emp.specialty,
        "skills": emp.skills,
        "divisions": emp.divisions,
        "permissions": emp.permissions,
        "provider_id": emp.provider_id,
        "model_name": emp.model_name,
        "provider_account_id": emp.provider_account_id,
        "provider_session_id": emp.provider_session_id,
        "browser_profile_id": emp.browser_profile_id,
        "workspace_path": emp.workspace_path,
        "autonomy_mode": emp.autonomy_mode,
        "status": emp.status,
    }


@router.post("/employees/skill-gaps", dependencies=[Depends(require_role("admin", "operator"))])
async def analyze_employee_skill_gaps(
    body: EmployeeSkillGapRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await analyze_team_skill_gaps(db, body.required_skills, divisions=body.divisions)


@router.post("/employees/provision-specialists", status_code=201)
async def provision_employee_specialists(
    body: EmployeeSkillGapRequest,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    return await provision_missing_specialists(
        db,
        body.required_skills,
        divisions=body.divisions,
        actor=user.email,
    )


@router.post("/employees/{agent_id}/replace", dependencies=[Depends(require_role("admin", "operator"))])
async def replace_employee_endpoint(
    agent_id: str,
    reason: str = "Unavailable or repeated failures",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await replacement_service.replace_employee(db, agent_id, reason=reason, actor=user.email)
    except NoFreeWorkerError as exc:
        # 409: the request is valid, but there is no free worker to take over (the manager never does).
        return JSONResponse(
            status_code=409,
            content={
                "detail": " / ".join(exc.messages.values()),
                "code": "no_free_worker",
                "agent_id": exc.failing_agent_id,
                "messages": exc.messages,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/employees/requeue-manager-tasks", dependencies=[Depends(require_role("admin", "operator"))])
async def requeue_manager_tasks_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Move the tasks assigned to the manager back to `queued` so a worker can pick them up.

    Explicit operator command: it is never run automatically by the scheduler, the QA engine or
    the Arena runner. It reports how many tasks were moved back to the queue.
    """
    return await replacement_service.requeue_manager_tasks(db, actor=user.email)
