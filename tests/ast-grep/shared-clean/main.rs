// Code the shared RFC-0018 rules accept: native arguments, typed errors.
fn main() {
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    std::process::exit(if args.is_empty() { 2 } else { 0 });
}
