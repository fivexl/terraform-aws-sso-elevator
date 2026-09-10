package main

import "testing"

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
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := helpRequested(c.args); got != c.want {
				t.Errorf("helpRequested(%v) = %v, want %v", c.args, got, c.want)
			}
		})
	}
}
