package main

import (
	"flag"
	"testing"
)

// newRequestFlagSet builds a FlagSet with the same flags runRequest defines,
// without pulling in runRequest itself -- helpRequested only needs the flag
// definitions (names and whether each is boolean) to tell a flag token from
// the value that follows it.
func newRequestFlagSet() *flag.FlagSet {
	fs := flag.NewFlagSet("elevator", flag.ContinueOnError)
	fs.String("account", "", "")
	fs.String("permission-set", "", "")
	fs.String("duration", "", "")
	fs.String("reason", "", "")
	fs.String("endpoint", "", "")
	fs.String("region", "", "")
	return fs
}

// TestHelpRequested is a regression test (#194 D5): -h/-help/--help must be
// recognized no matter where in the argument list it appears, not just as
// the first argument -- that's what lets exitIfHelpRequested intercept it
// before FlagSet.Parse ever sees it, so both `elevator --help` (handled by
// main's own top-level switch) and `elevator --account 123456789012 --help`
// (handled here, since it isn't os.Args[1]) print the identical usage text
// to the identical stream (stdout), instead of the latter falling through
// to FlagSet.Parse's own usage handling on fs.Output(), which defaults to
// stderr.
func TestHelpRequested(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want bool
	}{
		{name: "no args", args: []string{}, want: false},
		{name: "-h as the only arg", args: []string{"-h"}, want: true},
		{name: "-help as the only arg", args: []string{"-help"}, want: true},
		{name: "--help as the only arg", args: []string{"--help"}, want: true},
		{name: "--help after other flags", args: []string{"--account", "123456789012", "--help"}, want: true},
		{name: "-h in the middle", args: []string{"--account", "123456789012", "-h", "--reason", "x"}, want: true},
		{name: "no help flag present", args: []string{"--account", "123456789012", "--permission-set", "Foo"}, want: false},
		// Guards against a naive substring/prefix check instead of an exact
		// match -- a flag that merely starts with "-h" or contains "help"
		// must not be mistaken for a help request.
		{name: "a flag value containing \"help\" is not a help request", args: []string{"--reason", "please help me understand this"}, want: false},
		{name: "a flag merely prefixed with -h is not a help request", args: []string{"-hello"}, want: false},
		// Regression (#194, found live by Andrey Devyatkin): "--help" as the
		// literal *value* of a preceding value-taking flag must not be
		// mistaken for a help request -- that silently printed usage and
		// exited 0 without ever submitting the request, reporting a false
		// success for a request that was never sent.
		{name: "--help as a flag's own value is not a help request", args: []string{"--account", "123456789012", "--reason", "--help"}, want: false},
		{name: "--help as a flag's value via = is not a help request", args: []string{"--reason=--help"}, want: false},
		{name: "a real --help still works after a flag whose value looks like a flag", args: []string{"--reason", "--account", "--help"}, want: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := helpRequested(newRequestFlagSet(), c.args); got != c.want {
				t.Errorf("helpRequested(%v) = %v, want %v", c.args, got, c.want)
			}
		})
	}
}
