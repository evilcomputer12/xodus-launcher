# Xbox GDK runtime (xgameruntime.dll) coverage

Games built with the Xbox Game Development Kit load `xgameruntime.dll` and ask it for runtime
classes by CLSID (`QueryApiImpl`). Microsoft's real DLL (Gaming Services 38.117) provides 24
classes. This table tracks what the open-source WineGDK build used by Xodus Launcher provides.

| Class | WineGDK upstream | Xodus Launcher patches | Notes |
|---|---|---|---|
| XThreadingImpl (XAsync/XTaskQueue) | yes | fixed 224-byte overrun, trace crash | core of every async call |
| XSystemImpl | partial (v1) | v2-v5, sandbox override `WINE_XBL_SANDBOX` | |
| XSystemAnalyticsImpl | yes | | |
| XGameRuntimeFeatureImpl | yes | | |
| XNetworkingImpl | yes | | connectivity hint stubs |
| XUserImpl | stubs | offline local player, gamertag, privileges | real Xbox Live sign-in planned via xodus-service |
| XGameImpl | no | added (title id from MicrosoftGame.config) | |
| XGameSaveImpl | no | added, local files under `AppData\Local\XodusGames\<titleid>\GameSave` | no cloud sync |
| XPersistentLocalStorageImpl | no | added | |
| XStoreImpl, XPackageImpl, XGameUiImpl, XErrorImpl, XGameEventImpl, XDisplayImpl, XLauncherImpl, XUserDeviceImpl, XAccessibilityImpl, XAppCaptureImpl, XAppCaptureMetadataImpl, XGameInviteImpl, XGameProtocolImpl, XGameActivationImpl, XGameStreamingImpl | no | being ported from the xodus-gaming wine fork (stubs) | |

## Real Xbox Live sign-in (planned)

The Xbox app and games obtain service tokens through `XUserGetTokenAndSignatureAsync`
(`{Token, Signature}` for a URL). xodus already implements the XSTS token flow and carries the
endpoint-to-relying-party table; the plan is a `TokenAndSignature` request in `xodus-service`
and a WineGDK implementation that calls it over the existing `xodus.sock` IPC.

## Debugging a game that fails to start

Run it with `WINEDEBUG=fixme+xgameruntime,trace+gdkc` and look for
`QueryApiImpl {guid} not implemented` lines; the GUID identifies the missing class
(see the IDL files under `include/` in WineGDK).
