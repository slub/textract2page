import click

from .convert_aws import convert_file

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option(
    "-O",
    "--output-file",
    default="-",
    help='Output filename (or "-" for standard output)',
    type=click.Path(dir_okay=False, writable=True, exists=False, allow_dash=True),
)
@click.argument("aws-json-file", type=click.Path(dir_okay=False, exists=True))
@click.argument("image-file", type=click.Path(dir_okay=False, exists=True))
def cli(output_file, aws_json_file, image_file):
    """Convert an AWS Textract JSON file to a PAGE XML file.

    Also requires the original input image of AWS OCR to get absolute image coordinates.

    The output file will reference the image file under `Page/@imageFilename`
    with its full path. (So you may want to use a relative path.)
    """
    if output_file == "-":
        output_file = None
    convert_file(aws_json_file, image_file, output_file)


if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter
