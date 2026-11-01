// Without a Developer ID (VOICE_REC_ADHOC=1, see packaging/build_app.sh) sign the app
// here: with the self-signed certificate VOICE_REC_SIGN_IDENTITY (the same identity in
// every build, so macOS keeps the app's permissions across updates), else ad hoc.
// Apple Silicon refuses unsigned code, and a bundle whose seal was broken by
// packaging shows as "damaged" instead of "unidentified developer".
const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function afterPack(context) {
  if (process.env.VOICE_REC_ADHOC !== '1' || context.electronPlatformName !== 'darwin') return;
  const appPath = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const identity = process.env.VOICE_REC_SIGN_IDENTITY || '-';
  console.log(`  • ${identity === '-' ? 'ad-hoc signing' : `signing with "${identity}"`} ${appPath}`);
  execFileSync('codesign', ['--force', '--deep', '--sign', identity, '--timestamp=none',
    '--entitlements', path.join(__dirname, 'entitlements.mac.plist'), appPath], { stdio: 'inherit' });
};
