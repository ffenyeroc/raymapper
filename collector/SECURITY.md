# Security policy

This is the device-side uploader for Raymap. What counts as a security bug here:

- A hardware identifier, SIM identity, phone number or precise coordinate leaving the device
  in an upload, a log line or an error message.
- The upload path skipping proof of work, or the device pseudonym becoming stable across
  reinstalls.
- Anything that makes the collector dump raw `qmdl` content off the device.

Mapping and server issues belong in the map/API code at the repository root.

## Reporting

Use **GitHub's private vulnerability reporting** (*Security* tab → *Report a
vulnerability*), not a public issue. Say which version you are running (the release tag or git
commit), the device model and the log line.

Expect an acknowledgement within a week; there is no paid on-call beyond that.
