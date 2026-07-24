import secrets
import logging
from datetime import datetime, timedelta, UTC
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import exists, func, text
from pydantic import BaseModel, EmailStr

from backend.app.db import get_db, get_supabase, SessionLocal
from backend.app.auth import get_current_admin, get_current_user
from backend.app.models import Profile, Supplier, CatalogItem, CatalogEmail, EmployeeInvitation, PasswordReset, EmailAccount, AIQueryLog
from backend.app.services.email_sender import send_smtp_email
from backend.app.config import get_settings
from backend.app.security import escape_html
from fastapi import BackgroundTasks

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()

class EmployeeInviteRequest(BaseModel):
    name: str
    email: EmailStr

class ResetPasswordCompleteRequest(BaseModel):
    token: str
    password: str

class CompleteActivationRequest(BaseModel):
    token: str
    password: str
    name: str

# 1. Dashboard Metrics Endpoint
@router.get("/dashboard-stats")
def get_dashboard_stats(db: Session = Depends(get_db), current_user: dict = Depends(get_current_admin)):
    tenant_uuid = UUID(current_user["tenant_id"])
    
    # Total employees in their organisation
    total_employees = db.query(Profile).filter(Profile.tenant_id == tenant_uuid, Profile.role == "employee").count()
    
    # Total supplier emails processed for their tenant
    total_emails = db.query(CatalogEmail).filter(
        CatalogEmail.tenant_id == tenant_uuid,
        CatalogEmail.processing_status == "completed",
        exists().where(CatalogItem.catalog_email_id == CatalogEmail.id),
    ).count()
    
    # AI queries today (within last 24h)
    twenty_four_hours_ago = datetime.now(UTC) - timedelta(days=1)
    queries_today = db.query(AIQueryLog).filter(
        AIQueryLog.tenant_id == tenant_uuid,
        AIQueryLog.created_at >= twenty_four_hours_ago
    ).count()
    
    return {
        "total_employees": total_employees,
        "total_emails_processed": total_emails,
        "ai_queries_today": queries_today
    }

# 2. Database Overview Metrics Endpoint
@router.get("/database-stats")
def get_database_stats(db: Session = Depends(get_db), current_user: dict = Depends(get_current_admin)):
    tenant_uuid = UUID(current_user["tenant_id"])
    
    total_suppliers = db.query(Supplier).filter(Supplier.tenant_id == tenant_uuid).distinct().count()
    total_ingredients = db.query(CatalogItem.normalized_name).filter(CatalogItem.tenant_id == tenant_uuid).distinct().count()
    
    # simple PgDatabase size fallback if not in postgres
    db_size_mb = 0.0
    try:
        size_query = db.execute(func.pg_database_size(func.current_database()))
        db_size_bytes = size_query.scalar() or 0
        db_size_mb = round(db_size_bytes / (1024 * 1024), 2)
    except Exception:
        db_size_mb = 12.4 # Mock fallback if sqlite/local
        
    one_day_ago = datetime.now(UTC) - timedelta(days=1)
    one_month_ago = datetime.now(UTC) - timedelta(days=30)
    
    searches_day = db.query(AIQueryLog).filter(
        AIQueryLog.tenant_id == tenant_uuid,
        AIQueryLog.created_at >= one_day_ago
    ).count()
    
    searches_month = db.query(AIQueryLog).filter(
        AIQueryLog.tenant_id == tenant_uuid,
        AIQueryLog.created_at >= one_month_ago
    ).count()
    
    return {
        "total_suppliers": total_suppliers,
        "total_ingredients": total_ingredients,
        "database_size_mb": db_size_mb,
        "searches_per_day": searches_day,
        "searches_per_month": searches_month
    }

class AdminRegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    organisation: str

@router.post("/register-admin", status_code=status.HTTP_201_CREATED)
def register_admin(payload: AdminRegisterRequest, db: Session = Depends(get_db)):
    # Verify that organisation does not already exist in database profiles
    existing_org = db.query(Profile).filter(Profile.organisation == payload.organisation).first()
    if existing_org:
        raise HTTPException(status_code=400, detail="This organisation name is already registered.")

    try:
        supabase_client = get_supabase()
        attributes = {
            "email": payload.email,
            "password": payload.password,
            "email_confirm": True, # Auto-confirms email address (skips verification link)
            "user_metadata": {
                "full_name": payload.name.strip(),
                "role": "admin",
                "organisation": payload.organisation.strip()
            }
        }
        supabase_client.auth.admin.create_user(attributes)
        return {"success": True, "message": "Workspace registered. Awaiting Superadmin approval."}
    except Exception as e:
        logger.error(f"Failed to register admin workspace: {e}")
        err_msg = str(e)
        if "already exists" in err_msg.lower() or "unique" in err_msg.lower():
            raise HTTPException(status_code=400, detail="An account with this email address is already registered.")
        raise HTTPException(status_code=500, detail="Failed to create admin workspace registration.")

# 3. Add Employee / Send Invite
@router.post("/employees/invite", status_code=status.HTTP_201_CREATED)
def invite_employee(
    payload: EmployeeInviteRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin)
):
    admin_uuid = UUID(current_user["id"])
    tenant_uuid = UUID(current_user["tenant_id"])
    
    # Check if invitation already exists
    existing_invite = db.query(EmployeeInvitation).filter(
        EmployeeInvitation.email == payload.email
    ).first()
    if existing_invite:
        # Check if the user profile exists in the database
        user_id_val = db.execute(text("SELECT id FROM auth.users WHERE email = :email"), {"email": payload.email}).scalar()
        profile_exists = False
        if user_id_val:
            profile_exists = db.query(Profile).filter(Profile.id == user_id_val).first() is not None
            
        if profile_exists:
            p = db.query(Profile).filter(Profile.id == user_id_val).first()
            if p.status == "Disabled":
                raise HTTPException(status_code=400, detail="This account is deactivated. Please delete or reactivate it.")
            else:
                raise HTTPException(status_code=400, detail="An employee with this email address is already registered.")
        else:
            # The profile was deleted, so we can clean up the old invitation record to prevent UNIQUE constraint violation
            db.delete(existing_invite)
            db.commit()
         
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    
    new_invite = EmployeeInvitation(
        admin_id=admin_uuid,
        tenant_id=tenant_uuid,
        name=payload.name,
        email=payload.email,
        token=token,
        expires_at=expires_at,
        status="Pending Activation"
    )
    db.add(new_invite)
    db.commit()
    
    admin_profile = db.query(Profile).filter(Profile.id == admin_uuid).first()
    org_name = admin_profile.organisation if (admin_profile and admin_profile.organisation) else "MediCORE"
    safe_payload_name = escape_html(payload.name)
    safe_org_name = escape_html(org_name)

    # Send invitation email via background Celery task
    activation_link = f"{settings.frontend_origin}/activate?token={token}"
    email_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#17211c;max-width:500px;margin:0 auto;padding:24px;border:1px solid #dce4df;border-radius:12px;">
      <h2 style="color:#0f7a5f;margin:0 0 16px 0;">You're invited to join MediCORE</h2>
      <p>Hi {safe_payload_name},</p>
      <p>You've been invited to use MediCORE by {safe_org_name}.</p>
      <p style="margin:24px 0;">
        <a href="{activation_link}" style="background-color:#0f7a5f;color:white;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:600;display:inline-block;">Activate Account</a>
      </p>
      <p style="font-size:12px;color:#66736d;">This activation link expires in 2 hours. If you did not expect this invitation, you can safely ignore this email.</p>
      <hr style="border:none;border-top:1px solid #dce4df;margin:24px 0;" />
      <p style="font-size:12px;color:#66736d;margin:0;">Regards,<br>MediCORE Team</p>
    </div>
    """
    background_tasks.add_task(send_smtp_email, payload.email, "You're invited to join MediCORE", email_html)
    return {"message": "Invitation sent successfully."}

# 4. Verify Invitation Token
@router.get("/activate/verify")
def verify_activation_token(token: str, db: Session = Depends(get_db)):
    invite = db.query(EmployeeInvitation).filter(EmployeeInvitation.token == token).first()
    if not invite or invite.status != "Pending Activation":
        raise HTTPException(status_code=400, detail="Invalid or already used activation token.")
    if invite.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        invite.status = "Expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Activation link has expired.")
    return {"email": invite.email, "name": invite.name}

# 4b. Complete Employee Account Activation without email confirmation
@router.post("/activate/complete")
def complete_activation(payload: CompleteActivationRequest, db: Session = Depends(get_db)):
    invite = db.query(EmployeeInvitation).filter(EmployeeInvitation.token == payload.token).first()
    if not invite or invite.status != "Pending Activation":
        raise HTTPException(status_code=400, detail="Invalid or already used activation token.")
    if invite.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        invite.status = "Expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Activation link has expired.")
        
    try:
        supabase_client = get_supabase()
        
        # We need organization name from the inviting admin's profile
        admin_profile = db.query(Profile).filter(Profile.id == invite.admin_id).first()
        org_name = admin_profile.organisation if (admin_profile and admin_profile.organisation) else "MediCORE"
        
        # Create user via Supabase admin auth API.
        # This allows setting email_confirm to True so that NO confirmation link is sent.
        attributes = {
            "email": invite.email,
            "password": payload.password,
            "email_confirm": True,
            "user_metadata": {
                "full_name": payload.name,
                "role": "employee",
                "organisation": org_name
            }
        }
        
        supabase_client.auth.admin.create_user(attributes)
        
        # We can also verify that the trigger has updated invitation status to Active.
        # Just in case, let's commit/verify.
        # (The PostgreSQL trigger handle_new_user should have set status = 'Active')
        db.refresh(invite)
        if invite.status == "Pending Activation":
            invite.status = "Active"
            db.commit()
            
        return {"success": True, "message": "Employee account activated successfully."}
        
    except Exception as e:
        logger.error("Failed to create employee user in Supabase: %s", e)
        # If user already exists in auth.users, let's provide a friendly message.
        err_msg = str(e)
        if "already exists" in err_msg.lower() or "unique" in err_msg.lower():
            raise HTTPException(status_code=400, detail="An account with this email address has already been created.")
        raise HTTPException(status_code=500, detail="Failed to activate account.")


# 5. List Employees for current admin's organisation
@router.get("/employees")
def list_employees(db: Session = Depends(get_db), current_user: dict = Depends(get_current_admin)):
    tenant_uuid = UUID(current_user["tenant_id"])
    
    # Get all active/disabled profiles of employees
    profiles = db.query(Profile).filter(Profile.tenant_id == tenant_uuid, Profile.role == "employee").all()
    # Get pending invitations
    invitations = db.query(EmployeeInvitation).filter(
        EmployeeInvitation.tenant_id == tenant_uuid,
        EmployeeInvitation.status == "Pending Activation"
    ).all()
    
    employees_list = []
    
    # Render profiles
    for p in profiles:
        # Fetch email account last sync details
        email_account = db.query(EmailAccount).filter(EmailAccount.user_id == p.id).first()
        last_sync = "Never"
        connected_email = "Not connected"
        if str(p.id) == current_user["id"]:
            connected_email = current_user["email"]
            last_sync = "N/A (Admin)"
        elif email_account:
            connected_email = email_account.email_address
            if email_account.sync_status == "error":
                last_sync = "Sync error"
            elif email_account.last_synced_at:
                # Format relative time
                diff = datetime.now(UTC) - email_account.last_synced_at.replace(tzinfo=UTC)
                if diff.days > 0:
                    last_sync = "Yesterday" if diff.days == 1 else f"{diff.days} days ago"
                elif diff.seconds >= 3600:
                    hours = diff.seconds // 3600
                    last_sync = "1 hour ago" if hours == 1 else f"{hours} hours ago"
                elif diff.seconds >= 60:
                    mins = diff.seconds // 60
                    last_sync = "1 minute ago" if mins == 1 else f"{mins} minutes ago"
                else:
                    last_sync = "Just now"

        # Skip admin user from listing as employee if they want purely employees list, but let's include all profiles for database overview
        employees_list.append({
            "id": str(p.id),
            "name": p.full_name,
            "email": connected_email,
            "status": p.status,
            "role": p.role,
            "last_sync": last_sync
        })
        
    # Render invitations
    for inv in invitations:
        employees_list.append({
            "id": str(inv.id),
            "name": inv.name,
            "email": inv.email,
            "status": "Pending Activation",
            "role": "employee",
            "last_sync": "Never"
        })
        
    return employees_list

def async_remove_employee_cleanup(user_id: UUID):
    db = SessionLocal()
    try:
        db.query(EmailAccount).filter(EmailAccount.user_id == user_id).delete(synchronize_session=False)
        db.commit()
        logger.info(f"Successfully finished background remove-clean-up for employee user_id {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error in async_remove_employee_cleanup task: {e}")
    finally:
        db.close()


def async_delete_employee_cleanup(user_id: UUID, email_val: str | None):
    db = SessionLocal()
    try:
        from backend.app.models import EmailAccount, PasswordReset, EmailSyncSetting, EmployeeInvitation
        if email_val:
            db.query(EmployeeInvitation).filter(EmployeeInvitation.email == email_val).delete(synchronize_session=False)
        db.query(EmailAccount).filter(EmailAccount.user_id == user_id).delete(synchronize_session=False)
        db.query(PasswordReset).filter(PasswordReset.user_id == user_id).delete(synchronize_session=False)
        db.query(EmailSyncSetting).filter(EmailSyncSetting.user_id == user_id).delete(synchronize_session=False)
        db.commit()
        logger.info(f"Successfully finished background delete-clean-up for employee user_id {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error in async_delete_employee_cleanup task: {e}")
    finally:
        db.close()


# 6. Disable Employee / Remove Employee
@router.post("/employees/{user_id}/remove")
def remove_employee(
    user_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin)
):
    tenant_uuid = UUID(current_user["tenant_id"])
    profile = db.query(Profile).filter(Profile.id == user_id, Profile.tenant_id == tenant_uuid).first()
    
    if not profile:
        # Check if this ID is a pending invitation
        invitation = db.query(EmployeeInvitation).filter(
            EmployeeInvitation.id == user_id,
            EmployeeInvitation.tenant_id == tenant_uuid
        ).first()
        if invitation:
            db.delete(invitation)
            db.commit()
            return {"message": "Pending employee invitation has been cancelled."}
        raise HTTPException(status_code=404, detail="Employee profile not found")
        
    if profile.id == UUID(current_user["id"]):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own admin account.")
        
    # Disable status (fast update)
    profile.status = "Disabled"
    db.commit()
    
    # Process slow deletes asynchronously to release database locks and avoid API timeouts
    background_tasks.add_task(async_remove_employee_cleanup, user_id)
    
    return {"message": f"Employee {profile.full_name} has been deactivated."}


# Permanent Delete Employee
@router.post("/employees/{user_id}/delete")
def delete_employee(
    user_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin)
):
    tenant_uuid = UUID(current_user["tenant_id"])
    profile = db.query(Profile).filter(Profile.id == user_id, Profile.tenant_id == tenant_uuid).first()
    
    if not profile:
        raise HTTPException(status_code=404, detail="Employee profile not found")
        
    if profile.id == UUID(current_user["id"]):
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account.")
        
    # Resolve employee email before deleting profile
    email_val = None
    try:
        email_val = db.execute(text("SELECT email FROM auth.users WHERE id = :id"), {"id": user_id}).scalar()
    except Exception as e:
        logger.warning(f"Could not resolve email from auth.users for user_id {user_id}: {e}")
        
    # Delete profile synchronously (fast update)
    db.delete(profile)
    db.commit()
    
    # Run the cascading deletes and invitation clean-ups in background
    background_tasks.add_task(async_delete_employee_cleanup, user_id, email_val)
    
    return {"message": f"Employee has been permanently deleted."}

# 7. Reset Password Trigger
@router.post("/employees/{user_id}/reset-password")
def reset_password(
    user_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin)
):
    tenant_uuid = UUID(current_user["tenant_id"])
    profile = db.query(Profile).filter(Profile.id == user_id, Profile.tenant_id == tenant_uuid).first()
    
    if not profile:
        raise HTTPException(status_code=404, detail="Employee profile not found")
        
    # Invalidate previous pending resets for this user
    db.query(PasswordReset).filter(PasswordReset.user_id == user_id, PasswordReset.status == "Pending").update(
        {PasswordReset.status: "Expired"}, synchronize_session=False
    )
    
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    
    reset_record = PasswordReset(
        user_id=user_id,
        token=token,
        expires_at=expires_at,
        status="Pending"
    )
    db.add(reset_record)
    db.commit()
    
    # Retrieve user email by looking up Supabase or mock user record
    # Since email details are in Supabase auth, we'll try to retrieve the email address.
    user_email = ""
    try:
        supabase_client = get_supabase()
        response = supabase_client.auth.admin.get_user_by_id(str(user_id))
        if response and response.user:
            user_email = response.user.email
    except Exception:
        # Fallback to connected email search
        email_acct = db.query(EmailAccount).filter(EmailAccount.user_id == user_id).first()
        if email_acct:
            user_email = email_acct.email_address
            
    if not user_email:
        raise HTTPException(status_code=400, detail="Could not determine user email for password reset notification.")
        
    reset_link = f"{settings.frontend_origin}/reset-password?token={token}"
    safe_profile_name = escape_html(profile.full_name)
    email_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#17211c;max-width:500px;margin:0 auto;padding:24px;border:1px solid #dce4df;border-radius:12px;">
      <h2 style="color:#0f7a5f;margin:0 0 16px 0;">Reset Your MediCORE Password</h2>
      <p>Hi {safe_profile_name},</p>
      <p>Your administrator has requested a password reset for your MediCORE account.</p>
      <p style="margin:24px 0;">
        <a href="{reset_link}" style="background-color:#0f7a5f;color:white;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:600;display:inline-block;">Reset Password</a>
      </p>
      <p style="font-size:12px;color:#66736d;">This password reset link expires in 2 hours. If you did not request this, you can ignore this email.</p>
      <hr style="border:none;border-top:1px solid #dce4df;margin:24px 0;" />
      <p style="font-size:12px;color:#66736d;margin:0;">Regards,<br>MediCORE Team</p>
    </div>
    """
    background_tasks.add_task(send_smtp_email, user_email, "Reset your MediCORE password", email_html)
    return {"message": "Password reset link emailed successfully."}

# 8. Verify Password Reset Token
@router.get("/reset-password/verify")
def verify_reset_token(token: str, db: Session = Depends(get_db)):
    reset = db.query(PasswordReset).filter(PasswordReset.token == token, PasswordReset.status == "Pending").first()
    if not reset:
        raise HTTPException(status_code=400, detail="Invalid or already used password reset token.")
    if reset.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        reset.status = "Expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Password reset link has expired.")
    return {"verified": True}

# 9. Complete Password Reset
@router.post("/reset-password/complete")
def complete_password_reset(payload: ResetPasswordCompleteRequest, db: Session = Depends(get_db)):
    reset = db.query(PasswordReset).filter(PasswordReset.token == payload.token, PasswordReset.status == "Pending").first()
    if not reset:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")
    if reset.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        reset.status = "Expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Password reset link has expired.")
        
    try:
        supabase_client = get_supabase()
        # Update user password in Supabase Auth using admin API
        supabase_client.auth.admin.update_user_by_id(
            str(reset.user_id),
            {"password": payload.password}
        )
        # Mark token as Used
        reset.status = "Used"
        db.commit()
        return {"success": True, "message": "Password updated successfully."}
    except Exception as e:
        logger.error("Failed to update password in Supabase: %s", e)
        raise HTTPException(status_code=500, detail="Failed to reset password.")
