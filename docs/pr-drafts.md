# Upstream pull request drafts

Drafts for the user to approve before posting. Nothing here has been posted.

## 1. xodus-gaming/xodus — streaming download crash fix (branch `fix/prefix-cache-flush`)

**Title:** msixvc: flush PrefixCacheFile writes before exposing them to the reader

`xodus-cli streaming` panicked a few hundred KB into every download on my machine (CachyOS,
tokio file I/O) with `cache ended before cached_len` (UnexpectedEof). `PrefixCacheFile` advanced
`cached_len` as soon as `poll_write` returned, but `cache_writer` is a `tokio::fs::File` whose
writes complete in the background, so the separate `cache_reader` handle could read the region
before the bytes reached the file.

This adds a `Flushing` state to the cache write state machine: after a write, the writer is
flushed and only then is `cached_len` advanced and the pending chunk released. Tested by
downloading Balatro and DOOM: The Dark Ages end to end.

## 2. xodus-gaming/xodus — Xbox Live tokens and identity for games (branch `feat/token-and-signature`)

**Title:** xodus-service: TokenAndSignature and UserInfo requests for GDK titles

Games built with the GDK obtain Xbox Live service tokens through
`XUserGetTokenAndSignatureAsync(method, url, headers, body)` and expect
`{Token: "XBL3.0 x=<uhs>;<xsts>", Signature: "<XBL request signature>"}`, plus the signed-in
user's identity (XUID, gamertags, age group) from `XUser*`. This adds both to xodus-service's
socket protocol so a Wine-side `xgameruntime.dll` can serve them:

- `TokenAndSignatureRequest` (type 5) / `TokenAndSignatureResponse` (type 6): resolves the
  relying party and signature policy from the title endpoint table, obtains an XSTS token for
  it through the title-bound sisu session (cached per client/title/relying party), signs the
  request (ES256 "XBL" policy; body hashed up to the policy's `MaxBodyBytes`) and returns the
  authorization value, the signature and the identity claims (xid/gtg/mgt/mgs/umg/agg/uhs).
- `UserInfoRequest` (type 7) / `UserInfoResponse` (type 8): the identity alone, `SignedIn=false`
  when no user is stored.
- Error replies carry an HRESULT (`E_GAMEUSER_SIGNED_OUT`, `E_GAMEUSER_RESOLVE_USER_ISSUE_REQUIRED`,
  `E_INVALIDARG`, `E_FAIL`) so the runtime can map them 1:1.
- Unit tests for relying-party resolution, request signing (cross-checked against xal's signer),
  XML (de)serialization and the age-group mapping.

Verified end to end with Balatro under WineGDK: the game signs in with the real account and
its achievements request receives a valid signed token.

Follow-ups worth mentioning in the PR: a request body larger than the 64 KB frame is signed
over its prefix (a longer length field or chunking would fix that); the reqwest client in
`simple_context.rs` has no timeout.

## 3. Weather-OS/WineGDK — branch `xodus-launcher` (12 commits)

The WineGDK README says the Xodus interoperability code is "not upstream safe", so this should
be split: the generic fixes can go to WineGDK, the xodus-specific pieces stay in our series.

Upstreamable (one PR, or one per commit):
- xgameruntime: Fix XAsyncBlock overrun (224 bytes past the caller's block) and the
  XAsyncSchedule trace crash; add the missing newline in xtaskqueue.h.
- twinapi.appcore: Return a registration from RegisterAppStateChangeNotification.
- xgameruntime: IXSystemImpl2-5, hex TitleId parsing.
- xgameruntime: XGame, XGameSave (local), XPersistentLocalStorage; the 15 stub classes ported
  from the xodus wine fork (LGPL, authorship kept in the commit message).
- xgameruntime: force-close watchdog for games that ignore WM_CLOSE.

Keep in our series (xodus-specific): the memfd loader (`ntdll: Support loading from memfd`,
from xodus-gaming/wine), the TokenAndSignature/UserInfo IPC messages and the XUser identity
code that talks to xodus-service.
