#![forbid(unsafe_code)]

fn main() {
    if let Err(error) = termdeck_web_server::run_from_env() {
        eprintln!("TermDeck web server failed: {error}");
        std::process::exit(1);
    }
}
