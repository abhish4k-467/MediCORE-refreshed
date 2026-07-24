alter table email_sync_settings
    alter column poll_interval_minutes set default 15;

update email_sync_settings
set poll_interval_minutes = 15
where poll_interval_minutes is null
   or poll_interval_minutes < 5;
