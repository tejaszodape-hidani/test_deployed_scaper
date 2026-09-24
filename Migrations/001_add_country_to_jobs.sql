-- Add country column
ALTER TABLE `Job`
ADD COLUMN `country` VARCHAR(100) NULL AFTER `jobLocation`;

-- Backfill existing records
UPDATE `Job`
SET `country` = 'US'
WHERE `country` IS NULL OR `country` = '';

-- Make country required
ALTER TABLE `Job`
MODIFY COLUMN `country` VARCHAR(100) NOT NULL;