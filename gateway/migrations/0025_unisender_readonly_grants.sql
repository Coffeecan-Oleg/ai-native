-- Preserve both existing training roles; allow exactly one additional read grant.
alter table platforma_reader_requests drop constraint platforma_approval_grant_count_check;
alter table platforma_reader_requests add constraint platforma_approval_grant_count_check
  check (status <> 'approved' or
    (approved_until is not null and cardinality(scope_grant_ids) in (6,7,9,10)
     and resource_grant_id is not null));
