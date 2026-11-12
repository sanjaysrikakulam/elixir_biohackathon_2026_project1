# Routing by Galaxy user id instead of e-mail

byoc-sync decides two things for every BYOC endpoint: which user owns it, and how that user's jobs reach it. Today both rely on the user's e-mail address. A TPV `users.yml` entry keyed by the address sends the user's jobs to their endpoint, and each generated destination has an owner guard that compares `user.email`. The same `users.yml` keys are byoc-sync's record of who owns which BYOC name.

That puts every BYOC user's address into the playbook repository and its pull requests. For a public repository that is a problem. This page describes a second mode that uses the Galaxy user id instead and writes no address anywhere. The current mode stays the default.

## How it works

The site setting `owner_key: galaxy_id` switches the mode. Three things change.

1. **Routing comes from one fixed TPV rule** that a Galaxy admin adds once ([`../galaxy-side/tpv/byoc_rule.yml`](../galaxy-side/tpv/byoc_rule.yml)). For each job it reads the BYOC name from the job owner's own preferences (`byoc_pulsar|username`). If a destination with that endpoint's tag exists, it requires that tag. The tag is `<name>-byoc-pulsar`, as in the default naming. byoc-sync writes no per-user routing entry.
2. **The destination's owner guard checks the user id.** The generated destination keeps its guard rule, but compares the job owner's id with the id of the user who registered the endpoint:

   ```yaml
   rules:
     - id: byoc-owner-guard
       if: user is None or "<id>" not in (str(user.id), app.security.encode_id(user.id))
       execute: |
         from tpv.core.entities import TryNextDestinationOrFail
         raise TryNextDestinationOrFail("pulsar_alice01_byoc_tpv only accepts jobs of its owner")
   ```

   The check accepts the encoded id (what the API source reports) and the database id (what the gxadmin source reports), so it works with either source.
3. **Ownership is kept in a small file keyed by user id**, for example `byoc_owners.yml`. Galaxy and TPV never read it. byoc-sync uses it the way it uses the `users.yml` keys today: to see which name belongs to whom, to detect renames and to remove an endpoint when its owner disables BYOC or is deleted.

```yaml
byoc_owners:
  # keeps the mapping non-empty so byoc-sync can add owners below it
  _placeholder: {}
  '0e0fad4d103ae8ed':
    name: alice01
```

byoc-sync only adds entries next to an existing one, so the file starts with the placeholder entry from [`../galaxy-side/byoc_owners.yml`](../galaxy-side/byoc_owners.yml). byoc-sync ignores it, because its key is not a user id. Keys are always quoted: an unquoted database id such as `42` would be read as a number.

## Setting it up

[`../galaxy-side/site-by-user-id.yml`](../galaxy-side/site-by-user-id.yml) is usegalaxy.eu's site file with these changes: `owner_key: galaxy_id`, the `users` target pointing at the owners file, and the templates in [`../galaxy-side/site-templates/`](../galaxy-side/site-templates/). Of those, `destination.yml.j2` has the id guard and `owner.yml.j2` writes the owner entry; the runner and RabbitMQ templates are usegalaxy.eu's. A site copies them, adapts the paths and adds the TPV rule.

## Why it is safe

The id comes from Galaxy's own record of the job, not from anything the user types. A user who enters someone else's name in the form gets that endpoint's tag added by the rule, but the destination's guard refuses the job, because the ids do not match. The job then fails with "only accepts jobs of its owner", and never runs on someone else's Pulsar. byoc-sync already refuses a name that another user owns, so this only happens when a user types a name that is not theirs.

Setting the form's name to empty stops routing at once, before the removal pull request is merged, because the rule no longer finds a name.

## What changed in byoc-sync

- A site setting `owner_key: email | galaxy_id`, default `email`. The planner uses this key wherever it used the e-mail: claims, owners, conflicts, the allow list (which accepts e-mails and ids) and the reports. With `galaxy_id` the plan, the reports and the pull requests show the user id, not the address.
- The `users` target is the ownership file when `owner_key` is `galaxy_id` (`id: owner_key`). Its entries name the endpoint with a plain `name:` field.
- The templates get `owner_key` and `galaxy_id`. With `galaxy_id`, `email` and `email_key` are empty, so no template can put an address into a file.
- Both sources look owners up by id (`lookup_id`): the API by encoded id, gxadmin by database id. Users whose data cannot be read are recorded by e-mail and id, so their entries stay untouched in either mode.
- Use one source per site: the API source reports encoded ids and the gxadmin source database ids, so switching sources would change every stored id.

usegalaxy.eu keeps its current setup. A site that wants addresses out of its repository sets `owner_key: galaxy_id`, adds the TPV rule once and uses a destination template with the id guard.
