package org.allenai.scienceparse2

import org.apache.pdfbox.tools.PDFToImage

object PDFRenderer {
  // see org.apache.pdfbox.tools.PDFToImage for all possible arguments.
  trait CommandConfig

  case class PDFRendererConfig(
    command: String = null,
    commandConfig: CommandConfig = null)

  case class PDFToImageConfig(
    format: Option[String] = None,
    prefix: Option[String] = None,
    startPage: Option[Int] = None,
    endPage: Option[Int] = None,
    dpi: Option[Int] = None,
    inputfile: String = null)
      extends CommandConfig

  case class PreprocessPdfConfig(
    outputFileName: String = null,
    inputNames: Seq[String] = Seq())
      extends CommandConfig


  val parser = new scopt.OptionParser[PDFRendererConfig]("PDFRenderer") {
    cmd("PDFToImage")
      .action((_, c) => c.copy(command = "PDFToImage", commandConfig = PDFToImageConfig()))
      .text("Render and save to disk PDF pages as image files")
      .children(
        opt[String]('f', "format")
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(format = Some(x)))
          }),
        opt[String]('p', "prefix")
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(prefix = Some(x)))
          }),
        opt[Int]('s', "startPage")
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(startPage = Some(x)))
          }),
        opt[Int]('e', "endPage")
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(endPage = Some(x)))
          }),
        opt[Int]('d', "dpi")
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(dpi = Some(x)))
          }),
        arg[String]("inputfile")
          .required()
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PDFToImageConfig].copy(inputfile = x))
          }))
    cmd("PreprocessPdf")
      .action((_, c) => c.copy(command = "PreprocessPdf", commandConfig = PreprocessPdfConfig()))
      .text("Extract text and other information from the PDF")
      .children(
        arg[String]("outputFileName")
          .required()
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PreprocessPdfConfig].copy(outputFileName = x))
          }),
        arg[Seq[String]]("inputNames")
          .required()
          .unbounded()
          .action((x, c) => {
            c.copy(
              commandConfig = c.commandConfig.asInstanceOf[PreprocessPdfConfig].copy(
                inputNames = c.commandConfig.asInstanceOf[PreprocessPdfConfig].inputNames ++ x))
          }))
    checkConfig { c => c match {
      case PDFRendererConfig(null, _) => failure("Please specify a command")
      case _ => success
    }}
  }

  def main(args: Array[String]): Unit = {
    parser.parse(args, PDFRendererConfig()) match {
      case Some(config) => {
        if (config.command == "PDFToImage") {
          // only use scopt for option validation, but
          // allow PDFToImage to conduct it's own option
          // parsing
          PDFToImage.main(args.drop(1))
        } else if (config.command == "PreprocessPdf") {
          PreprocessPdf.extractText(
            outputFileName = config.commandConfig.asInstanceOf[PreprocessPdfConfig].outputFileName,
            inputNames = config.commandConfig.asInstanceOf[PreprocessPdfConfig].inputNames)
        }
      }
      case None => System.exit(1)
    }
  }
}
