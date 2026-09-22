import { Command, CommanderError } from 'commander'

import { Cancelled, CliError, EXIT } from './lib/errors.ts'
import { c, err, isTTY, setColour } from './lib/output.ts'
import { configCommand } from './commands/config.ts'
import { doctorCommand } from './commands/doctor.ts'
import { endpointCommand } from './commands/endpoint.ts'
import { galleryCommand } from './commands/gallery.ts'
import { genCommand } from './commands/gen.ts'
import { instanceCommand } from './commands/instance.ts'
import { jobCommand } from './commands/job.ts'
import { webCommand } from './commands/web.ts'

const VERSION = '0.1.0'

function build(): Command {
  const program = new Command('cv')
    .description('comfy-vast: rent the GPU, drive the endpoint, render, and look at the results')
    .version(VERSION, '-v, --version')
    .showHelpAfterError('(run `cv --help`)')
    .configureHelp({ sortSubcommands: false })

  program.addCommand(instanceCommand())
  program.addCommand(endpointCommand())
  program.addCommand(jobCommand())
  program.addCommand(genCommand())
  program.addCommand(galleryCommand())
  program.addCommand(webCommand())
  program.addCommand(configCommand())
  program.addCommand(doctorCommand())

  program.addHelpText(
    'after',
    `
Examples:
  cv doctor                              check the toolchain and the live endpoint
  cv instance search --limit 5           offers matching VAST_SEARCH_PARAMS
  cv endpoint scale --max 1 --cold 0     stop paying between renders
  cv gen "1girl, standing" --discord     render and post each image as it lands
  cv job submit "1girl" --no-upscale     the same pipeline through the local API
  cv web up --tunnel                     the browser UI, published

Every command takes --json. Configuration comes from .env at the repository root.
`,
  )

  return program
}

/** Print an error the way a CLI should: to stderr, with the fix if there is one. */
function report(e: unknown): number {
  if (e instanceof Cancelled) {
    err(c.dim(e.message))
    return e.code
  }
  if (e instanceof CliError) {
    err(`${c.red('error')} ${e.message}`)
    if (e.hint) err(`${c.dim('hint ')} ${e.hint}`)
    if (e.details) err(c.dim(typeof e.details === 'string' ? e.details : JSON.stringify(e.details, null, 2)))
    return e.code
  }
  if (e instanceof Error) {
    err(`${c.red('error')} ${e.message}`)
    if (process.env['CV_DEBUG'] && e.stack) err(c.dim(e.stack))
    else err(c.dim('set CV_DEBUG=1 for a stack trace'))
    return EXIT.ERROR
  }
  err(`${c.red('error')} ${String(e)}`)
  return EXIT.ERROR
}

export async function main(argv: string[] = process.argv): Promise<void> {
  setColour(isTTY())

  // Broken pipes are normal for a CLI: `cv gallery ls | head` closes stdout
  // while output is still being written. Exiting quietly beats a stack trace.
  process.stdout.on('error', (e: NodeJS.ErrnoException) => {
    if (e.code === 'EPIPE') process.exit(0)
  })

  const program = build()
  program.exitOverride()

  try {
    await program.parseAsync(argv)
  } catch (e) {
    if (e instanceof CommanderError) {
      // --help and --version reach here as "errors"; they are not.
      if (e.code === 'commander.helpDisplayed' || e.code === 'commander.help' || e.code === 'commander.version') {
        process.exitCode = EXIT.OK
        return
      }
      process.exitCode = e.exitCode === 0 ? EXIT.OK : EXIT.USAGE
      return
    }
    process.exitCode = report(e)
  }
}

await main()
