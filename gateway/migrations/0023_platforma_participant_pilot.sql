-- Preserve legacy reader approvals; permit the exact nine-grant training role.
do $$
declare guard_name text;
begin
    select conname into strict guard_name from pg_constraint
    where conrelid='platforma_reader_requests'::regclass and contype='c'
      and pg_get_constraintdef(oid) like '%cardinality(scope_grant_ids) = 6%';
    execute format('alter table platforma_reader_requests drop constraint %I',guard_name);
    alter table platforma_reader_requests add constraint platforma_approval_grant_count_check
      check (status <> 'approved' or
        (approved_until is not null and cardinality(scope_grant_ids) in (6,9)
         and resource_grant_id is not null));
end $$;
