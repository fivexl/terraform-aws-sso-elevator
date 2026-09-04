module github.com/fivexl/terraform-aws-sso-elevator/cmd/elevator

go 1.24

// go 1.24 above is the minimum for building this module from source (the
// lowest version the AWS SDK dependencies themselves require) -- it does
// NOT mean release builds should actually be compiled with a 1.24.x
// toolchain. `go 1.24` alone, with GOTOOLCHAIN=auto's default behavior,
// resolves to whatever locally-available toolchain already satisfies
// "1.24 or newer", including an old 1.24.x patch -- confirmed locally that
// this resolves to go1.24.5, which govulncheck flags with 27 reachable
// standard-library vulnerabilities (several, e.g. in crypto/tls and
// net/http, only fixed in the 1.25.x line and never backported to 1.24.x,
// since Go only backports security fixes to its two most recent releases).
// This toolchain directive pins actual builds -- including
// cli-release.yml's, since it uses the same go-version-file -- to a
// current, fully-patched release regardless of what's locally cached.
//
// Dependabot's gomod entry for this module (.github/dependabot.yml) does
// NOT keep this current: Dependabot's gomod ecosystem only bumps `require`
// directives, and there is no Go-toolchain ecosystem for it to use instead
// (unlike e.g. rust-toolchain/dotnet-sdk, which Dependabot does support).
// This pin will silently rot -- there is currently no automated mechanism
// that re-checks it -- so bumping it periodically (or whenever govulncheck
// flags something only fixed in a newer line) needs to be a manual,
// deliberate step, not something to assume Dependabot is already doing.
toolchain go1.27.0

require (
	github.com/aws/aws-sdk-go-v2 v1.43.5
	github.com/aws/aws-sdk-go-v2/config v1.32.36
)

require (
	github.com/aws/aws-sdk-go-v2/credentials v1.19.35 // indirect
	github.com/aws/aws-sdk-go-v2/feature/ec2/imds v1.18.36 // indirect
	github.com/aws/aws-sdk-go-v2/internal/configsources v1.4.36 // indirect
	github.com/aws/aws-sdk-go-v2/internal/endpoints/v2 v2.7.36 // indirect
	github.com/aws/aws-sdk-go-v2/internal/v4a v1.4.37 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/accept-encoding v1.13.16 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/presigned-url v1.13.36 // indirect
	github.com/aws/aws-sdk-go-v2/service/signin v1.5.5 // indirect
	github.com/aws/aws-sdk-go-v2/service/sso v1.33.5 // indirect
	github.com/aws/aws-sdk-go-v2/service/ssooidc v1.38.5 // indirect
	github.com/aws/aws-sdk-go-v2/service/sts v1.45.5 // indirect
	github.com/aws/smithy-go v1.27.7 // indirect
)
