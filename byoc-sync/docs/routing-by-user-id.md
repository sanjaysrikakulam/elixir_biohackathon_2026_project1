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
# managed by byoc-sync
byoc_owners:
  0e0fad4d103ae8ed:
    name: alice01
```

## Why it is safe

The id comes from Galaxy's own record of the job, not from anything the user types. A user who enters someone else's name in the form gets that endpoint's tag added by the rule, but the destination's guard refuses the job, because the ids do not match. The job then fails with "only accepts jobs of its owner", and never runs on someone else's Pulsar. byoc-sync already refuses a name that another user owns, so this only happens when a user types a name that is not theirs.

Setting the form's name to empty stops routing at once, before the removal pull request is merged, because the rule no longer finds a name.

## What changes in byoc-sync

- A site setting `owner_key: email | galaxy_id`, default `email`. The planner uses this key where it uses the e-mail today: claims, owners, conflicts and reports.
- A target for the owners file, used instead of the `users` target when `owner_key` is `galaxy_id`. The site file leaves the `users` target out.
- The user id (`galaxy_id`) in the template context, so a site's destination template can write the owner guard above.
- One source per site. The API source reports encoded ids and the gxadmin source database ids, so switching sources would change every stored id.

usegalaxy.eu keeps its current setup. A site that wants addresses out of its repository sets `owner_key: galaxy_id`, adds the TPV rule once and uses a destination template with the id guard.
